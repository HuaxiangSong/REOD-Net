from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

import torch
import torch.nn.functional as F
from torch import nn, Tensor

from torchvision.ops.misc import Conv2dNormActivation
from torchvision.utils import _log_api_usage_once

#----------------------------------------------------------

class GSConv(nn.Module):
    # GSConv https://github.com/AlanLi1997/slim-neck-by-gsconv
    def __init__(self, in_channels, out_channels, kernel_size=3, kernel_size_gs=5, dropout=None):
        super().__init__()
        c_share = out_channels // 2
        self.cv1 = Conv2dNormActivation(
            in_channels, c_share, kernel_size=kernel_size, groups=1, norm_layer=None, activation_layer=None)
        self.cv2 = Conv2dNormActivation(
            c_share, c_share, kernel_size=kernel_size_gs,padding=kernel_size_gs//2,groups=c_share,
            norm_layer=None, activation_layer=nn.Mish, inplace=True)
        self.dropout = nn.Dropout(p=0.2, inplace=True)
        self.dp_tag = dropout
        
    def forward(self, x):
        x1 = self.cv1(x)
        x2 = torch.cat((x1, self.cv2(x1).sigmoid()), 1)
        # channel shuffle
        y = x2.reshape(x2.shape[0], 2, x2.shape[1] // 2, x2.shape[2], x2.shape[3])
        y = y.permute(0, 2, 1, 3, 4)
        return y.reshape(y.shape[0], -1, y.shape[3], y.shape[4]) if self.dp_tag is None else self.dropout(y.reshape(y.shape[0], -1, y.shape[3], y.shape[4]))
    
#---------------------------------------------------------------

class ExtraFPNBlock(nn.Module):
    """
    Base class for the extra block in the FPN.

    Args:
        results (List[Tensor]): the result of the FPN
        x (List[Tensor]): the original feature maps
        names (List[str]): the names for each one of the
            original feature maps

    Returns:
        results (List[Tensor]): the extended set of results
            of the FPN
        names (List[str]): the extended set of names for the results
    """

    def forward(
        self,
        results: List[Tensor],
        x: List[Tensor],
        names: List[str],
    ) -> Tuple[List[Tensor], List[str]]:
        pass


class FeaturePyramidNetwork(nn.Module):
    """
    Module that adds a FPN from on top of a set of feature maps. This is based on
    `"Feature Pyramid Network for Object Detection" <https://arxiv.org/abs/1612.03144>`_.

    The feature maps are currently supposed to be in increasing depth
    order.

    The input to the model is expected to be an OrderedDict[Tensor], containing
    the feature maps on top of which the FPN will be added.

    Args:
        in_channels_list (list[int]): number of channels for each feature map that
            is passed to the module
        out_channels (int): number of channels of the FPN representation
        extra_blocks (ExtraFPNBlock or None): if provided, extra operations will
            be performed. It is expected to take the fpn features, the original
            features and the names of the original features as input, and returns
            a new list of feature maps and their corresponding names
        norm_layer (callable, optional): Module specifying the normalization layer to use. Default: None

    Examples::

        >>> m = torchvision.ops.FeaturePyramidNetwork([10, 20, 30], 5)
        >>> # get some dummy data
        >>> x = OrderedDict()
        >>> x['feat0'] = torch.rand(1, 10, 64, 64)
        >>> x['feat2'] = torch.rand(1, 20, 16, 16)
        >>> x['feat3'] = torch.rand(1, 30, 8, 8)
        >>> # compute the FPN on top of x
        >>> output = m(x)
        >>> print([(k, v.shape) for k, v in output.items()])
        >>> # returns
        >>>   [('feat0', torch.Size([1, 5, 64, 64])),
        >>>    ('feat2', torch.Size([1, 5, 16, 16])),
        >>>    ('feat3', torch.Size([1, 5, 8, 8]))]

    """

    _version = 2

    def __init__(
        self,
        in_channels_list: List[int],
        out_channels: int,
        extra_blocks: Optional[ExtraFPNBlock] = None,
        norm_layer: Optional[Callable[..., nn.Module]] = None,
        dropout = None,
    ):
        super().__init__()
        _log_api_usage_once(self)
        self.inner_blocks = nn.ModuleList()
        self.layer_blocks = nn.ModuleList()
        self.split_blocks_0 = nn.ModuleList()
        self.split_blocks_1 = nn.ModuleList()
        self.dropout = nn.Dropout(p=0.2, inplace=True)
        for in_channels in in_channels_list:
            if in_channels == 0:
                raise ValueError("in_channels=0 is currently not supported")
            inner_block_module = GSConv(
                in_channels, out_channels, kernel_size=1, kernel_size_gs=5, dropout = None)
            layer_block_module = GSConv(
                out_channels, out_channels, kernel_size=3, kernel_size_gs=5, dropout = dropout)
            split_block_module_0 = GSConv(
                in_channels//4, in_channels//4, kernel_size=5, kernel_size_gs=7)
            split_block_module_1 = GSConv(
                in_channels//4, in_channels//4, kernel_size=7, kernel_size_gs=9)
            self.inner_blocks.append(inner_block_module)
            self.layer_blocks.append(layer_block_module)
            self.split_blocks_0.append(split_block_module_0)
            self.split_blocks_1.append(split_block_module_1)

        # initialize parameters now to avoid modifying the initialization of top_blocks
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_uniform_(m.weight, a=1)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

        if extra_blocks is not None:
            if not isinstance(extra_blocks, ExtraFPNBlock):
                raise TypeError(f"extra_blocks should be of type ExtraFPNBlock not {type(extra_blocks)}")
        self.extra_blocks = extra_blocks

    def _load_from_state_dict(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        version = local_metadata.get("version", None)

        if version is None or version < 2:
            num_blocks = len(self.inner_blocks)
            for block in ["inner_blocks", "layer_blocks"]:
                for i in range(num_blocks):
                    for type in ["weight", "bias"]:
                        old_key = f"{prefix}{block}.{i}.{type}"
                        new_key = f"{prefix}{block}.{i}.0.{type}"
                        if old_key in state_dict:
                            state_dict[new_key] = state_dict.pop(old_key)

        super()._load_from_state_dict(
            state_dict,
            prefix,
            local_metadata,
            strict,
            missing_keys,
            unexpected_keys,
            error_msgs,
        )

    def get_result_from_inner_blocks(self, x: Tensor, idx: int) -> Tensor:
        """
        This is equivalent to self.inner_blocks[idx](x),
        but torchscript doesn't support this yet
        """
        num_blocks = len(self.inner_blocks)
        if idx < 0:
            idx += num_blocks
        out = x
        for i, module in enumerate(self.inner_blocks):
            if i == idx:
                out = module(x)
        return out

    def get_result_from_layer_blocks(self, x: Tensor, idx: int) -> Tensor:
        """
        This is equivalent to self.layer_blocks[idx](x),
        but torchscript doesn't support this yet
        """
        num_blocks = len(self.layer_blocks)
        if idx < 0:
            idx += num_blocks
        out = x
        for i, module in enumerate(self.layer_blocks):
            if i == idx:
                out = module(x)
        return out
    
    def get_result_from_split_blocks(self, x: Tensor, idx: int) -> Tensor:
        """
        This is equivalent to self.layer_blocks[idx](x),
        but torchscript doesn't support this yet
        """
        num_blocks = len(self.split_blocks_0)
        num_channels = x.shape[1]
        if idx < 0:
            idx += num_blocks
        out = x
        for i, module in enumerate(self.split_blocks_0):
            if i == idx:
                out_0 = module(x[:,:num_channels//4,]).sigmoid()
                out_1 = self.split_blocks_1[i](x[:,num_channels//4:num_channels//2,]).sigmoid()
                out_2 = x[:,num_channels//2:,]
                out = self.dropout(torch.concat((out_0,out_1,out_2),1))
        return out

    def forward(self, x: Dict[str, Tensor]) -> Dict[str, Tensor]:
        """
        Computes the FPN for a set of feature maps.

        Args:
            x (OrderedDict[Tensor]): feature maps for each feature level.

        Returns:
            results (OrderedDict[Tensor]): feature maps after FPN layers.
                They are ordered from the highest resolution first.
        """
        # unpack OrderedDict into two lists for easier handling
        names = list(x.keys())
        x = list(x.values())

        features_ahead_inner_blocks = self.get_result_from_split_blocks(x[-1], -1)
        last_inner = self.get_result_from_inner_blocks(features_ahead_inner_blocks, -1)
        #last_inner = self.get_result_from_inner_blocks(x[-1], -1)
        results = []
        results.append(self.get_result_from_layer_blocks(last_inner, -1))

        for idx in range(len(x) - 2, -1, -1):
            features_ahead_inner_blocks = self.get_result_from_split_blocks(x[idx], idx)
            inner_lateral = self.get_result_from_inner_blocks(features_ahead_inner_blocks, idx)
            #inner_lateral = self.get_result_from_inner_blocks(x[idx], idx)
            feat_shape = inner_lateral.shape[-2:]
            inner_top_down = F.interpolate(last_inner, size=feat_shape, mode="nearest")
            last_inner = inner_lateral + inner_top_down
            results.insert(0, self.get_result_from_layer_blocks(last_inner, idx))

        if self.extra_blocks is not None:
            results, names = self.extra_blocks(results, x, names)

        # make it back an OrderedDict
        out = OrderedDict([(k, v) for k, v in zip(names, results)])

        return out


class LastLevelMaxPool(ExtraFPNBlock):
    """
    Applies a max_pool2d (not actual max_pool2d, we just subsample) on top of the last feature map
    """

    def forward(
        self,
        x: List[Tensor],
        y: List[Tensor],
        names: List[str],
    ) -> Tuple[List[Tensor], List[str]]:
        names.append("pool")
        # Use max pooling to simulate stride 2 subsampling
        x.append(F.max_pool2d(x[-1], kernel_size=1, stride=2, padding=0))
        return x, names


class LastLevelP6P7(ExtraFPNBlock):
    """
    This module is used in RetinaNet to generate extra layers, P6 and P7.
    """

    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.p6 = nn.Conv2d(in_channels, out_channels, 3, 2, 1)
        self.p7 = nn.Conv2d(out_channels, out_channels, 3, 2, 1)
        for module in [self.p6, self.p7]:
            nn.init.kaiming_uniform_(module.weight, a=1)
            nn.init.constant_(module.bias, 0)
        self.use_P5 = in_channels == out_channels

    def forward(
        self,
        p: List[Tensor],
        c: List[Tensor],
        names: List[str],
    ) -> Tuple[List[Tensor], List[str]]:
        p5, c5 = p[-1], c[-1]
        x = p5 if self.use_P5 else c5
        p6 = self.p6(x)
        p7 = self.p7(F.relu(p6))
        p.extend([p6, p7])
        names.extend(["p6", "p7"])
        return p, names
