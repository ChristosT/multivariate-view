from pathlib import Path

import numba
import numpy as np

from trame.app import get_server
from trame.decorators import TrameApp, change
from trame.ui.vuetify3 import SinglePageWithDrawerLayout
from trame.widgets import client, vtk, vuetify3 as v
from trame_radvolviz.widgets import radvolviz

from .compute import (
    compute_gbc,
    data_topology_reduction,
    gbc_to_rgb,
    rotate_coordinates,
)
from .io import load_dataset
from .volume_view import VolumeView


# DATA_FILE = Path(__file__).parent.parent.parent / 'data/CeCoFeGd_doi_10.1038_s43246-022-00259-x.npz'
DATA_FILE = Path(__file__).parent.parent.parent / 'data/Ni_Mn_Co_O.vti'
# DATA_FILE = Path(__file__).parent.parent.parent / 'data/CoMnNiO.npz'


@TrameApp()
class App:
    def __init__(self, server=None):
        self.server = get_server(server, client_type='vue3')
        self.volume_view = VolumeView()

        self.unrotated_gbc = None
        self.unrotated_components = None

        self.gbc_data = None
        self.rgb_data = None
        self.first_render = True

        self.ui = self._build_ui()
        self.load_data()

    def load_data(self):
        header, data = load_dataset(DATA_FILE)

        self.state.component_labels = header

        # Remove padding so it will render faster.
        # This removes faces that are all zeros recursively until
        # the first non-zero voxel is hit.
        # Our sample data has a *lot* of padding.
        data = _remove_padding_uniform(data.astype(float))
        self.unpadded_data = data

        # Remember the data shape (without the multichannel part)
        self.data_shape = data.shape[:-1]
        self.num_channels = data.shape[-1]

        self.create_nonzero_data()

    # FIXME: these should be dynamic and potentially nested
    @change('w_min_0', 'w_max_0', 'w_min_1', 'w_max_1',
            'w_min_2', 'w_max_2', 'w_min_3', 'w_max_3')
    def create_nonzero_data(self, **kwargs):
        data = self.unpadded_data.copy()
        data = data.astype(np.float64)

        # Normalize
        for i in range(data.shape[-1]):
            data[:, :, :, i] = _normalize_data(data[:, :, :, i])

        # Cutoffs
        for i in range(data.shape[-1]):
            min_v = getattr(self.state, f'w_min_{i}')
            max_v = getattr(self.state, f'w_max_{i}')
            print(f'For {i}, {min_v=} {max_v=}')
            channel = data[:, :, :, i]
            threshold_cutoff = np.nanpercentile(channel, min_v)
            upper_cutoff = np.nanpercentile(channel, max_v)
            channel[channel < threshold_cutoff] = 0
            channel[channel > upper_cutoff] = upper_cutoff

        # Store the data in a flattened form. It is easier to work with.
        flattened_data = data.reshape(
            np.prod(self.data_shape), self.num_channels
        )
        self.nonzero_indices = ~np.all(np.isclose(flattened_data, 0), axis=1)

        # Only store nonzero data. We will reconstruct the zeros later.
        self.nonzero_unscaled_data = flattened_data[self.nonzero_indices]

        # Trigger an update of the data
        self.update_gbc()

    @property
    def nonzero_data(self):
        return self.nonzero_unscaled_data * self.rescale_factor

    @property
    def rescale_factor(self):
        # FIXME: these should be dynamic and potentially nested
        return [
            self.state.w_rescale_factor_0,
            self.state.w_rescale_factor_1,
            self.state.w_rescale_factor_2,
            self.state.w_rescale_factor_3,
        ]

    # FIXME: these should be dynamic and potentially nested
    @change('w_rescale_factor_0', 'w_rescale_factor_1', 'w_rescale_factor_2',
            'w_rescale_factor_3')
    def update_gbc(self, **kwargs):
        print(f'{self.rescale_factor=}')
        gbc, components = compute_gbc(self.nonzero_data)

        self.unrotated_gbc = gbc
        self.state.unrotated_component_coords = components.tolist()

        self.update_bin_data()
        self.update_voxel_colors()

    @change('w_bins', 'w_sample_size')
    def update_bin_data(self, **kwargs):
        num_samples = self.state.w_sample_size
        num_bins = self.state.w_bins

        # Perform random sampling
        self._sample_idx = np.random.choice(len(self.unrotated_gbc), size=num_samples)

        data = self.unrotated_gbc[self._sample_idx]
        unrotated_bin_data = data_topology_reduction(data, num_bins)
        self.state.unrotated_bin_data = unrotated_bin_data.tolist()

    @change('w_rotation')
    def update_voxel_colors(self, **kwargs):
        angle = np.radians(self.state.w_rotation)
        gbc = rotate_coordinates(self.unrotated_gbc, angle)

        self.gbc_data = gbc
        self.rgb_data = gbc_to_rgb(gbc)

        self.update_volume_data()

    def update_volume_data(self, **kwargs):
        if any(x is None for x in (self.rgb_data, self.gbc_data)):
            return

        rgb = self.rgb_data

        # Reconstruct full data with rgba values
        full_data = np.zeros((np.prod(self.data_shape), 4))
        full_data[self.nonzero_indices, :3] = rgb.T

        # Make nonzero voxels have an alpha of the mean of the channels.
        full_data[self.nonzero_indices, 3] = self.nonzero_data.mean(axis=1)
        full_data = full_data.reshape((*self.data_shape, 4))

        # Set the data on the volume
        self.volume_view.set_data(full_data)

        # Reset the camera if it is the first render
        self.reset_camera_on_first_render()

        # Update the mask data too. This will trigger an update.
        self.update_mask_data()

    @change('lens_center', 'w_lens', 'w_lradius', 'w_clip_x')
    def update_mask_data(self, **kwargs):
        if any(x is None for x in (self.rgb_data, self.gbc_data)):
            return

        alpha = self.compute_alpha()
        mask_ref = self.volume_view.mask_reference
        mask_ref[self.nonzero_indices] = alpha
        self.volume_view.mask_data.Modified()

        # Update the view
        self.ctrl.view_update()

    @property
    def state(self):
        return self.server.state

    @property
    def ctrl(self):
        return self.server.controller

    @property
    def render_window(self):
        return self.volume_view.render_window

    @property
    def lens_enabled(self):
        return self.state.w_lens

    def reset_camera_on_first_render(self):
        if not self.first_render:
            # Already had the first render
            return

        self.volume_view.renderer.ResetCameraClippingRange()
        self.volume_view.renderer.ResetCamera()
        self.ctrl.reset_camera()
        self.first_render = False

    def compute_alpha(self):
        gbc_data = self.gbc_data
        if gbc_data is None:
            # Can't do anything
            return None

        # Convert w_clip_x to a percentage
        clip_x = self.state.w_clip_x / 100
        if clip_x < 1:
            # Make a mask the shape of the original data
            clip_mask = np.ones(self.data_shape, dtype=bool)
            # Compute the max index, after which data is clipped
            max_idx = int(np.round(self.data_shape[0] * clip_x))
            # Apply clip
            clip_mask[max_idx:, :, :] = False
            # Reshape into the flat form and remove any zero index data
            clip_flattened = clip_mask.reshape(np.prod(self.data_shape))
            # If we perform any other operations, we can logical_and them
            alpha = clip_flattened[self.nonzero_indices]
        else:
            # All opaque
            alpha = np.ones(gbc_data.shape[0], dtype=bool)

        if not self.lens_enabled:
            # Only apply clipping
            return alpha

        # These are in unit circle coordinates
        r = self.state.w_lradius
        x, y = self.state.lens_center

        lens_alpha = _compute_alpha(np.array([x, y]), r, gbc_data)

        # Combine the lens alpha with the current alpha
        return np.logical_and(alpha, lens_alpha)

    def _build_ui(self):
        self.state.setdefault('lens_center', [0, 0])
        self.state.setdefault('w_clip_x', 100.0)

        # FIXME: maybe these should be nested variables?
        rescale_factors = [4.3201, 3.2501, 2.2501, 3.3601]
        mins = [94.3901, 92.3001, 93.5101, 95.7101]
        maxes = [100, 100, 100, 96.5601]
        for i in range(4):
            self.state.setdefault(f'w_rescale_factor_{i}', rescale_factors[i])
            self.state.setdefault(f'w_min_{i}', mins[i])
            self.state.setdefault(f'w_max_{i}', maxes[i])

        server = self.server
        ctrl = self.ctrl

        with SinglePageWithDrawerLayout(server, full_height=True) as layout:
            client.Style('html { overflow-y: hidden; }')

            with layout.toolbar.clear():
                v.VAppBarNavIcon(click='main_drawer = !main_drawer')
                v.VAppBarTitle('Multivariate')
                v.VSpacer()

            with layout.drawer as drawer:
                drawer.width = 400
                # add new widget
                v.VSlider(
                    label='Widget rotation',
                    v_model='w_rotation',
                    min=0,
                    max=360,
                    step=5,
                    density='compact',
                    hide_details=True,
                )
                v.VSlider(
                    label='Sample size',
                    v_model='w_sample_size',
                    min=100,
                    max=10000,
                    step=500,
                    density='compact',
                    hide_details=True,
                )
                v.VSlider(
                    label='Number of bins',
                    v_model='w_bins',
                    min=1,
                    max=10,
                    step=1,
                    density='compact',
                    hide_details=True,
                )
                v.VSlider(
                    label='Clip X',
                    v_model='w_clip_x',
                    min=0.0,
                    max=100.0,
                    step=0.001,
                    density='compact',
                    hide_details=True,
                )

                labels = ['Ni', 'Mn', 'Co', 'O']
                for i, label in enumerate(labels):
                    v.VSlider(
                        label=f'Rescale Factor {label}',
                        v_model=f'w_rescale_factor_{i}',
                        min=0.0001,
                        max=5.0,
                        step=0.01,
                        density='compact',
                        hide_details=True,
                    )

                for i, label in enumerate(labels):
                    v.VSlider(
                        label=f'Min: {label}',
                        v_model=f'w_min_{i}',
                        min=0.0001,
                        max=100.0,
                        step=0.01,
                        density='compact',
                        hide_details=True,
                    )
                    v.VSlider(
                        label=f'Max: {label}',
                        v_model=f'w_max_{i}',
                        min=0.0001,
                        max=100.0,
                        step=0.01,
                        density='compact',
                        hide_details=True,
                    )

                v.VSwitch(
                    label='Lens',
                    v_model='w_lens',
                )
                v.VSlider(
                    label='Lens radius',
                    v_model='w_lradius',
                    min=0.001,
                    max=1.0,
                    step=0.001,
                    density='compact',
                    hide_details=True,
                )

                radvolviz.NdColorMap(
                    component_labels=('component_labels', []),
                    unrotated_bin_data=('unrotated_bin_data', []),
                    unrotated_component_coords=(
                        'unrotated_component_coords',
                        [],
                    ),
                    size=drawer.width,
                    rotation=('w_rotation', 0),
                    sample_size=('w_sample_size', 6000),
                    number_of_bins=('w_bins', 6),
                    show_lens=('w_lens', False),
                    lens_radius=('w_lradius', 0.5),
                    lens='lens_center = $event',
                )

            with layout.content:
                html_view = vtk.VtkRemoteView(
                    self.render_window, interactive_ratio=1
                )

                ctrl.reset_camera = html_view.reset_camera
                ctrl.view_update = html_view.update


@numba.njit(cache=True, nogil=True)
def _compute_alpha(center, radius, gbc_data):
    # Compute distance formula to lens center
    distances = np.sqrt(((gbc_data - center) ** 2).sum(axis=1))

    # Any distances less than the radius are within the lens
    return distances < radius


@numba.njit(cache=True, nogil=True)
def _remove_padding_uniform(data: np.ndarray) -> np.ndarray:
    num_channels = data.shape[-1]
    zero_data = np.isclose(data, 0).sum(axis=3) == num_channels

    # This is the number to crop
    n = 0
    indices = np.array([n, -n - 1])
    while (
        zero_data[indices].all()
        & zero_data[:, indices].all()
        & zero_data[:, :, indices].all()
    ):
        n += 1
        indices = np.array([n, -n - 1])

    if n != 0:
        data = data[n : -n - 1, n : -n - 1, n : -n - 1]

    return data


@numba.njit(cache=True, nogil=True)
def _normalize_data(data: np.ndarray, new_min: float = 0, new_max: float = 1):
    max_val = data.max()
    min_val = data.min()

    return (new_max - new_min) * (data.astype(np.float64) - min_val) / (
        max_val - min_val
    ) + new_min
