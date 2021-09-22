# -*- coding: utf-8 -*-
##########################################################################
# NSAp - Copyright (C) CEA, 2021
# Distributed under the terms of the CeCILL-B license, as published by
# the CEA-CNRS-INRIA. Refer to the LICENSE file or to
# http://www.cecill.info/licences/Licence_CeCILL-B_V1-en.html
# for details.
##########################################################################

"""
The spherical UNet architecture.
"""

# Imports
from collections import namedtuple
import numpy as np
import torch
import torch.nn as nn
from joblib import Memory
from ..utils import (
    icosahedron, neighbors, number_of_ico_vertices, downsample, interpolate,
    neighbors_rec, get_logger, debug_msg)
from ..nn import (
    IcoUpConv, IcoMaxIndexUpSample, IcoFixIndexUpSample, IcoUpSample, IcoPool,
    IcoDiNeConv, IcoRePaConv)


# Global parameters
logger = get_logger()
Ico = namedtuple("Ico", ["order", "vertices", "triangles", "neighbor_indices",
                         "down_indices", "up_indices",
                         "conv_neighbor_indices"])


class SphericalUNet(nn.Module):
    """ The Spherical U-Net architecture.

    The architecture is built upon specific spherical surface convolution,
    pooling, and transposed convolution modules. It has an encoder path and
    a decoder path, with a user-defined resolution steps. Different from the
    standard U-Net, all 3×3 convolution are replaced with the RePa or DiNe
    convolution, 2×2 up-convolution with surface transposed convolution or
    surface upsampling, and 2×2 max pooling with surface max/mean pooling.
    In addition to the standard U-Net, before each convolution layer’s
    rectified linear units (ReLU) activation function, a batch normalization
    layer is added. At the final layer, 1×1 convolution is replaced by
    vertex-wise filter. The number of feature channels are double after each
    surface pooling layer and halve at each transposed convolution or up
    sampling layer.

    Notes
    -----
    Debuging messages can be displayed by changing the log level using
    ``setup_logging(level='debug')``.

    See Also
    --------
    IcoUpConv, IcoGenericUpConv, IcoUpSample, IcoFixIndexUpSample,
    IcoMaxIndexUpSample, IcoRePaConv, IcoDiNeConv, IcoPool

    Examples
    --------
    >>> import torch
    >>> from surfify.models import SphericalUNet
    >>> from surfify.utils import icosahedron
    >>> vertices, triangles = icosahedron(order=2)
    >>> model = SphericalUNet(
            in_order=2, in_channels=2, out_channels=4, depth=2,
            start_filts=8, conv_mode="1ring", up_mode="interp")
    >>> x = torch.zeros((10, 2, len(vertices)))
    >>> out = model(x)
    >>> out.shape

    References
    ----------
    .. [1] Zhao F, et al., Spherical U-Net on Cortical Surfaces: Methods and
       Applications, IPMI, 2019.
    """
    def __init__(self, in_order, in_channels, out_channels, depth=5,
                 start_filts=32, conv_mode="1ring", up_mode="interp",
                 cachedir=None):
        """ Init SphericalUNet.

        Parameters
        ----------
        in_order: int
            the input icosahedron order.
        in_channels: int
            input features/channels.
        out_channels: int
            output features/channels.
        depth: int, default 5
            number of layers in the UNet.
        start_filts: int, default 32
            number of convolutional filters for the first conv.
        conv_mode: str, default '1ring'
            the size of the spherical convolution filter: '1ring' or '2ring'.
            Can also use rectangular grid projection: 'repa'.
        up_mode: str, default 'interp'
            type of upsampling: 'transpose' for transpose
            convolution (1 ring), 'interp' for nearest neighbor linear
            interpolation, 'maxpad' for max pooling shifted zero padding,
            and 'zeropad' for classical zero padding.
        cachedir: str, default None
            set this folder to use smart caching speedup.
        """
        logger.debug("SphericalUNet init...")
        super(SphericalUNet, self).__init__()
        self.memory = Memory(cachedir, verbose=0)
        self.in_order = in_order
        self.depth = depth
        self.conv_mode = conv_mode
        self.in_vertices = number_of_ico_vertices(order=in_order)
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.up_mode = up_mode
        self.ico = {}
        icosahedron_cached = self.memory.cache(icosahedron)
        neighbors_cached = self.memory.cache(neighbors)
        neighbors_rec_cached = self.memory.cache(neighbors_rec)
        for order in range(1, in_order + 1):
            vertices, triangles = icosahedron_cached(order=order)
            logger.debug("- ico {0}: verts {1} - tris {2}".format(
                order, vertices.shape, triangles.shape))
            neighs = neighbors_cached(
                vertices, triangles, depth=1, direct_neighbor=True)
            neighs = np.asarray(list(neighs.values()))
            logger.debug("- neighbors {0}: {1}".format(
                order, neighs.shape))
            if conv_mode == "1ring":
                conv_neighs = neighs
                logger.debug("- neighbors {0}: {1}".format(
                    order, conv_neighs.shape))
            elif conv_mode == "2ring":
                conv_neighs = neighbors_cached(
                    vertices, triangles, depth=2, direct_neighbor=True)
                conv_neighs = np.asarray(list(conv_neighs.values()))
                logger.debug("- neighbors {0}: {1}".format(
                    order, conv_neighs.shape))
            elif conv_mode == "repa":
                conv_neighs, conv_weights, _ = neighbors_rec_cached(
                    vertices, triangles, size=5, zoom=5)
                logger.debug("- neighbors {0}: {1} - {2}".format(
                    order, conv_neighs.shape, conv_weights.shape))
                conv_neighs = (conv_neighs, conv_weights)
            else:
                raise ValueError("Unexptected convolution mode.")
            self.ico[order] = Ico(
                order=order, vertices=vertices, triangles=triangles,
                neighbor_indices=neighs, down_indices=None, up_indices=None,
                conv_neighbor_indices=conv_neighs)
        downsample_cached = self.memory.cache(downsample)
        for order in range(in_order, 1, -1):
            down_indices = downsample_cached(
                self.ico[order].vertices, self.ico[order - 1].vertices)
            logger.debug("- down {0}: {1}".format(order, down_indices.shape))
            self.ico[order] = self.ico[order]._replace(
                down_indices=down_indices)
        interpolate_cached = self.memory.cache(interpolate)
        for order in range(1, in_order):
            up_indices = interpolate_cached(
                self.ico[order].vertices, self.ico[order + 1].vertices,
                self.ico[order + 1].triangles)
            up_indices = np.asarray(list(up_indices.values()))
            logger.debug("- up {0}: {1}".format(order, up_indices.shape))
            self.ico[order] = self.ico[order]._replace(
                up_indices=up_indices)
        self.filts = [in_channels] + [
            start_filts * 2 ** idx for idx in range(depth)]
        logger.debug("- filters: {0}".format(self.filts))
        if conv_mode == "repa":
            self.sconv = IcoRePaConv
        else:
            self.sconv = IcoDiNeConv

        for idx in range(depth):
            order = self.in_order - idx
            logger.debug(
                "- DownBlock {0}: {1} -> {2} [{3} - {4} - {5}]".format(
                    idx, self.filts[idx], self.filts[idx + 1],
                    self.ico[order].neighbor_indices.shape,
                    (None if idx == 0
                        else self.ico[order + 1].neighbor_indices.shape),
                    (None if idx == 0
                        else self.ico[order + 1].down_indices.shape)))
            block = DownBlock(
                conv_layer=self.sconv,
                in_ch=self.filts[idx],
                out_ch=self.filts[idx + 1],
                conv_neigh_indices=self.ico[order].conv_neighbor_indices,
                down_neigh_indices=(
                    None if idx == 0
                    else self.ico[order + 1].neighbor_indices),
                down_indices=(
                    None if idx == 0
                    else self.ico[order + 1].down_indices),
                pool_mode=("max" if self.up_mode == "maxpad" else "mean"),
                first=(True if idx == 0 else False))
            setattr(self, "down{0}".format(idx + 1), block)

        cnt = 1
        for idx in range(depth - 1, 0, -1):
            logger.debug("- UpBlock {0}: {1} -> {2} [{3} - {4}]".format(
                cnt, self.filts[idx + 1], self.filts[idx],
                self.ico[order + 1].neighbor_indices.shape,
                self.ico[order].up_indices.shape))
            block = UpBlock(
                conv_layer=self.sconv,
                in_ch=self.filts[idx + 1],
                out_ch=self.filts[idx],
                conv_neigh_indices=self.ico[order + 1].conv_neighbor_indices,
                neigh_indices=self.ico[order + 1].neighbor_indices,
                up_neigh_indices=self.ico[order].up_indices,
                down_indices=self.ico[order + 1].down_indices,
                up_mode=self.up_mode)
            setattr(self, "up{0}".format(cnt), block)
            order += 1
            cnt += 1

        logger.debug("- FC: {0} -> {1}".format(self.filts[1], out_channels))
        self.fc = nn.Sequential(
            nn.Linear(self.filts[1], out_channels))

    def forward(self, x):
        """ Forward method.
        """
        logger.debug("SphericalUNet...")
        logger.debug(debug_msg("input", x))
        if x.size(2) != self.in_vertices:
            raise RuntimeError("Input data must be projected on an {0} order "
                               "icosahedron.".format(self.in_order))
        encoder_outs = []
        pooling_outs = []
        for idx in range(1, self.depth + 1):
            down_block = getattr(self, "down{0}".format(idx))
            logger.debug("- filter {0}: {1}".format(idx, down_block))
            x, max_pool_indices = down_block(x)
            encoder_outs.append(x)
            pooling_outs.append(max_pool_indices)
        encoder_outs = encoder_outs[::-1]
        pooling_outs = pooling_outs[::-1]
        for idx in range(1, self.depth):
            up_block = getattr(self, "up{0}".format(idx))
            logger.debug("- filter {0}: {1}".format(idx, up_block))
            x_up = encoder_outs[idx]
            max_pool_indices = pooling_outs[idx - 1]
            x = up_block(x, x_up, max_pool_indices)
        logger.debug("FC...")
        logger.debug(debug_msg("input", x))
        n_samples = len(x)
        x = x.permute(0, 2, 1)
        x = x.reshape(n_samples * self.in_vertices, self.filts[1])
        x = self.fc(x)
        x = x.view(n_samples, self.in_vertices, self.out_channels)
        x = x.permute(0, 2, 1)
        logger.debug(debug_msg("output", x))
        return x


class DownBlock(nn.Module):
    """ Downsampling block in spherical UNet:
    mean pooling => (conv => BN => ReLU) * 2
    """
    def __init__(self, conv_layer, in_ch, out_ch, conv_neigh_indices,
                 down_neigh_indices, down_indices, pool_mode="mean",
                 first=False):
        """ Init DownBlock.

        Parameters
        ----------
        conv_layer: nn.Module
            the convolutional layer on icosahedron discretized sphere.
        in_ch: int
            input features/channels.
        out_ch: int
            output features/channels.
        conv_neigh_indices: array
            conv layer's filters' neighborhood indices at sampling i.
        down_neigh_indices: array
            conv layer's filters' neighborhood indices at sampling i + 1.
        down_indices: array
            downsampling indices at sampling i.
        pool_mode: str, default 'mean'
            the pooling mode: 'mean' or 'max'.
        first: bool, default False
            if set skip the pooling block.
        """
        super(DownBlock, self).__init__()
        self.first = first
        if not first:
            self.pooling = IcoPool(
                down_neigh_indices, down_indices, pool_mode)
        self.block = nn.Sequential(
            conv_layer(in_ch, out_ch, conv_neigh_indices),
            nn.BatchNorm1d(out_ch, momentum=0.15, affine=True,
                           track_running_stats=False),
            nn.LeakyReLU(0.2, inplace=True),
            conv_layer(out_ch, out_ch, conv_neigh_indices),
            nn.BatchNorm1d(out_ch, momentum=0.15, affine=True,
                           track_running_stats=False),
            nn.LeakyReLU(0.2, inplace=True))

    def forward(self, x):
        """ Forward method.
        """
        logger.debug("- DownBlock")
        logger.debug(debug_msg("input", x))
        max_pool_indices = None
        if not self.first:
            x, max_pool_indices = self.pooling(x)
            logger.debug(debug_msg("pooling", x))
            if max_pool_indices is not None:
                logger.debug(debug_msg("max pooling indices",
                                       max_pool_indices))
        x = self.block(x)
        logger.debug(debug_msg("output", x))
        return x, max_pool_indices


class UpBlock(nn.Module):
    """ Define the upsamping block in spherical UNet:
    upconv => (conv => BN => ReLU) * 2
    """
    def __init__(self, conv_layer, in_ch, out_ch, conv_neigh_indices,
                 neigh_indices, up_neigh_indices, down_indices, up_mode):
        """ Init UpBlock.

        Parameters
        ----------
        conv_layer: nn.Module
            the convolutional layer on icosahedron discretized sphere.
        in_ch: int
            input features/channels.
        out_ch: int
            output features/channels.
        conv_neigh_indices: tensor, int
            conv layer's filters' neighborhood indices at sampling i.
        neigh_indices: tensor, int
            neighborhood indices at sampling i.
        up_neigh_indices: array
            upsampling neighborhood indices at sampling i + 1.
        down_indices: array
            downsampling indices at sampling i.
        up_mode: str, default 'interp'
            type of upsampling: 'transpose' for transpose
            convolution, 'interp' for nearest neighbor linear interpolation,
            'maxpad' for max pooling shifted zero padding, and 'zeropad' for
            classical zero padding.
        """
        super(UpBlock, self).__init__()
        self.up_mode = up_mode
        if up_mode == "interp":
            self.up = IcoUpSample(in_ch, out_ch, up_neigh_indices)
        elif up_mode == "zeropad":
            self.up = IcoFixIndexUpSample(in_ch, out_ch, up_neigh_indices)
        elif up_mode == "maxpad":
            self.up = IcoMaxIndexUpSample(
                in_ch, out_ch, neigh_indices, down_indices)
        elif up_mode == "transpose":
            self.up = IcoUpConv(
                in_ch, out_ch, neigh_indices, down_indices)
        else:
            raise ValueError("Invalid upsampling method.")
        self.double_conv = nn.Sequential(
             conv_layer(in_ch, out_ch, conv_neigh_indices),
             nn.BatchNorm1d(out_ch, momentum=0.15, affine=True,
                            track_running_stats=False),
             nn.LeakyReLU(0.2, inplace=True),
             conv_layer(out_ch, out_ch, conv_neigh_indices),
             nn.BatchNorm1d(out_ch, momentum=0.15, affine=True,
                            track_running_stats=False),
             nn.LeakyReLU(0.2, inplace=True))

    def forward(self, x1, x2, max_pool_indices):
        """ Forward method.
        """
        logger.debug("- UpBlock")
        logger.debug(debug_msg("input", x1))
        logger.debug(debug_msg("skip", x2))
        if self.up_mode == "maxpad":
            x1 = self.up(x1, max_pool_indices)
        else:
            x1 = self.up(x1)
        logger.debug(debug_msg("upsampling", x1))
        x = torch.cat((x1, x2), 1)
        logger.debug(debug_msg("cat", x))
        x = self.double_conv(x)
        logger.debug(debug_msg("output", x))
        return x
