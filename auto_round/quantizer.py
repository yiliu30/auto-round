
# Adapted from FlexRound's supplementary material
# https://openreview.net/forum?id=-tYCaP0phY_ 
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Union
from dataclasses import dataclass
from .utils import logger
class StraightThrough(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, input):
        return input


def round_ste(x: torch.Tensor):
    """
    Implement Straight-Through Estimator for rounding operation.
    """
    return (x.round() - x).detach() + x


def lp_loss(pred, tgt, p=2.0, reduction='none'):
    """
    loss function measured in L_p Norm
    """
    if reduction == 'none':
        return (pred - tgt).abs().pow(p).sum(1).mean()
    else:
        return (pred - tgt).abs().pow(p).mean()



@dataclass
class QuantizerConfig:
    # FlexRound only change the `n_bits`, `prob` and `leaf_param`
    n_bits: int = 8
    symmetric: bool = False
    channel_wise: bool = False
    scale_method: str = 'minmax'
    leaf_param: bool = False
    prob: float = 1.0
    use_ada: bool = False
    
    def to_dict(self):
        return {
            'n_bits': self.n_bits,
            'symmetric': self.symmetric,
            'channel_wise': self.channel_wise,
            'scale_method': self.scale_method,
            'leaf_param': self.leaf_param,
            'prob': self.prob
        }

default_quantizer_config = QuantizerConfig()
ada_default_quantizer_config = QuantizerConfig(use_ada=True)


class WUniformAffineQuantizer(nn.Module):
    """
    PyTorch Function that can be used for asymmetric quantization (also called uniform affine
    quantization). Quantizes its argument in the forward pass, passes the gradient 'straight
    through' on the backward pass, ignoring the quantization that occurred.
    Based on https://arxiv.org/abs/1806.08342.

    :param n_bits: number of bit for quantization
    :param symmetric: if True, the zero_point should always be 0
    :param channel_wise: if True, compute scale and zero_point in each channel
    :param scale_method: determines the quantization scale and zero point
    :param prob: for qdrop;
    """

    def __init__(self, n_bits: int = 8, symmetric: bool = False, channel_wise: bool = False,
                 scale_method: str = 'minmax',
                 leaf_param: bool = False, prob: float = 1.0):
        super(WUniformAffineQuantizer, self).__init__()
        self.sym = symmetric
        assert 2 <= n_bits <= 8, 'bitwidth not supported'
        self.n_bits = n_bits
        self.n_levels = 2 ** self.n_bits
        self.delta = 1.0
        self.delta1 = None
        self.delta2 = None
        self.delta3 = None
        self.delta4 = None
        self.zero_point = 0.0
        self.inited = True

        '''if leaf_param, use EMA to set scale'''
        self.leaf_param = leaf_param
        self.channel_wise = channel_wise
        self.scale_method = scale_method

        '''for activation quantization'''
        self.running_min = None
        self.running_max = None

        '''do like dropout'''
        self.prob = prob
        self.is_training = False

    def set_inited(self, inited: bool = True):  
        self.inited = inited

    def update_quantize_range(self, x_min: float, x_max: float):
        if self.running_min is None:
            self.running_min = x_min
            self.running_max = x_max
        self.running_min = 0.1 * x_min + 0.9 * self.running_min
        self.running_max = 0.1 * x_max + 0.9 * self.running_max
        x_min = self.running_min
        x_max = self.running_max
        return x_min, x_max
    
    
        # if self.inited is False:
        #     # TODO: move it into `__init__`
        #     if self.leaf_param:
        #         self.delta, self.zero_point = self.init_quantization_scale(x.clone().detach(), self.channel_wise)
        #     else:
        #         delta, self.zero_point = self.init_quantization_scale(x, self.channel_wise)
        #         self.delta1 = torch.nn.Parameter(torch.log(torch.tensor(delta)).clone()) 
        #         self.delta2 = torch.nn.Parameter(torch.zeros_like(x)) 
        #         if x.dim() >= 4:
        #             self.delta3 = torch.nn.Parameter(torch.zeros_like(x[:, 0, 0, 0]).unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)) 
        #         else:
        #             self.delta3 = torch.nn.Parameter(torch.zeros_like(x[:, 0].unsqueeze(-1))) 
        #         if x.dim() >= 4:
        #             self.delta4 = torch.nn.Parameter(torch.zeros_like(x[0, :, 0, 0]).unsqueeze(0).unsqueeze(-1).unsqueeze(-1))
        #     self.inited = True
    @classmethod
    def init_from_tensor(cls, x, config: QuantizerConfig, only_delta=False):
        quantizer = cls(**config.to_dict())
        if quantizer.leaf_param:
            quantizer.delta, quantizer.zero_point = quantizer.init_quantization_scale(x.clone().detach(), quantizer.channel_wise)
        else:
            delta, quantizer.zero_point = quantizer.init_quantization_scale(x, quantizer.channel_wise)
            # TODO: is ok?
            delta = torch.tensor(delta, device=x.device, dtype=x.dtype)
            quantizer.delta = delta
            if only_delta:
                logger.warning("Only delta is initialized")
                return quantizer
            quantizer.delta1 = torch.nn.Parameter(torch.log(delta.clone().detach())) 
            quantizer.delta2 = torch.nn.Parameter(torch.zeros_like(x)) 
            if x.dim() >= 4:
                quantizer.delta3 = torch.nn.Parameter(torch.zeros_like(x[:, 0, 0, 0]).unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)) 
            else:
                quantizer.delta3 = torch.nn.Parameter(torch.zeros_like(x[:, 0].unsqueeze(-1))) 
            if x.dim() >= 4:
                quantizer.delta4 = torch.nn.Parameter(torch.zeros_like(x[0, :, 0, 0]).unsqueeze(0).unsqueeze(-1).unsqueeze(-1))
        return quantizer

    def forward(self, x: torch.Tensor):
        # TODO: current we only support Linear
        assert x.dim() < 4, f"not support dim > 4, but got tensor with shape: {x.shape}"
        # if self.inited is False:
        #     # TODO: move it into `__init__`
        #     if self.leaf_param:
        #         self.delta, self.zero_point = self.init_quantization_scale(x.clone().detach(), self.channel_wise)
        #     else:
        #         delta, self.zero_point = self.init_quantization_scale(x, self.channel_wise)
        #         self.delta1 = torch.nn.Parameter(torch.log(torch.tensor(delta)).clone()) 
        #         self.delta2 = torch.nn.Parameter(torch.zeros_like(x)) 
        #         if x.dim() >= 4:
        #             self.delta3 = torch.nn.Parameter(torch.zeros_like(x[:, 0, 0, 0]).unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)) 
        #         else:
        #             self.delta3 = torch.nn.Parameter(torch.zeros_like(x[:, 0].unsqueeze(-1))) 
        #         if x.dim() >= 4:
        #             self.delta4 = torch.nn.Parameter(torch.zeros_like(x[0, :, 0, 0]).unsqueeze(0).unsqueeze(-1).unsqueeze(-1))
        #     self.inited = True
        # x_int = round_ste(x / (self.delta1 + self.delta2 + self.delta3 + self.delta4).exp()) if x.dim() >= 4 else round_ste(x / (self.delta1 + self.delta2 + self.delta3).exp())
        delta_sum = (self.delta1 + self.delta2 + self.delta3)
        delta_sum_exp = delta_sum.exp()
        x_int = round_ste(x / delta_sum_exp)
        x_quant = torch.clamp(x_int, - 2 ** (self.n_bits - 1), 2 ** (self.n_bits - 1) - 1) 
        x_dequant = x_quant * self.delta1.exp()
        x_dequant = x_dequant.to(x.dtype)

        if self.is_training and self.prob < 1.0:
            x_ans = torch.where(torch.rand_like(x) < self.prob, x_dequant, x)
        else:
            x_ans = x_dequant
        return x_ans
    
    def get_trainable_params(self):
        return [self.delta1,self.delta2, self.delta3]

    def get_x_min_x_max(self, x, x_min: float, x_max: float):
        if 'max' in self.scale_method:
            if 'scale' in self.scale_method:
                x_min = x_min * (self.n_bits + 2) / 8
                x_max = x_max * (self.n_bits + 2) / 8
            if self.leaf_param:
                x_min, x_max = self.update_quantize_range(x_min, x_max)
            x_absmax = max(abs(x_min), x_max)
            if self.sym:
                x_min, x_max = -x_absmax if x_min < 0 else 0, x_absmax
            return x_min, x_max
        elif self.scale_method == 'mse':
            best_score = 1e+10
            best_min, best_max = x_min, x_max
            for i in range(80):
                new_max = x_max * (1.0 - (i * 0.01))
                new_min = x_min * (1.0 - (i * 0.01))
                x_q = self.quantize(x, new_max, new_min)
                score = lp_loss(x, x_q, 2.4, reduction='all')
                if score < best_score:
                    best_score = score
                    best_min, best_max = new_min, new_max
            x_min, x_max = best_min, best_max
            if self.leaf_param:
                x_min, x_max = self.update_quantize_range(x_min, x_max)
            return x_min, x_max
        else:
            raise NotImplementedError

    def init_quantization_scale_channel(self, x: torch.Tensor):
        x_min, x_max = x.min().item(), x.max().item()
        x_min, x_max = self.get_x_min_x_max(x, x_min, x_max)
        if not self.leaf_param:
            delta = 2 * max(x_max, abs(x_min)) / (2 ** self.n_bits - 1) 
        else:
            delta = 2 * max(x_max, abs(x_min)) / (2 ** self.n_bits - 1) if x_min < 0 else x_max / (2 ** self.n_bits - 1)
        delta = max(delta, 1e-8)
        zero_point = 0

        return delta, zero_point

    def init_quantization_scale(self, x_clone: torch.Tensor, channel_wise: bool = False):
        if channel_wise:
            n_channels = x_clone.shape[0]
            if len(x_clone.shape) == 4:
                x_max = x_clone.abs().max(dim=-1)[0].max(dim=-1)[0].max(dim=-1)[0]
            else:
                x_max = x_clone.abs().max(dim=-1)[0]
            delta = x_max.clone()
            zero_point = x_max.clone()
            for c in range(n_channels):
                delta[c], zero_point[c] = self.init_quantization_scale_channel(x_clone[c])
            if len(x_clone.shape) == 4:
                delta = delta.view(-1, 1, 1, 1)
                zero_point = zero_point.view(-1, 1, 1, 1)
            else:
                delta = delta.view(-1, 1)
                zero_point = zero_point.view(-1, 1)
        else:
            delta, zero_point = self.init_quantization_scale_channel(x_clone)

        return delta, zero_point

    def quantize(self, x: torch.Tensor, x_max: float, x_min: float):
        if not self.leaf_param:
            delta = 2 * max(x_max, abs(x_min)) / (2 ** self.n_bits - 1) 
            x_int = torch.round(x / delta)
            x_quant = torch.clamp(x_int, - 2 ** (self.n_bits - 1), 2 ** (self.n_bits - 1) - 1)
        else:
            delta = 2 * max(x_max, abs(x_min)) / (2 ** self.n_bits - 1) if x_min < 0 else x_max / (2 ** self.n_bits - 1)
            x_int = torch.round(x / delta)
            x_quant = torch.clamp(x_int, - 2 ** (self.n_bits - 1), 2 ** (self.n_bits - 1) - 1) if x_min < 0 else torch.clamp(x_int, 0, 2 ** self.n_bits - 1)
        x_float_q = x_quant * delta

        return x_float_q

    def bitwidth_refactor(self, refactored_bit: int):
        assert 2 <= refactored_bit <= 8, 'bitwidth not supported'
        self.n_bits = refactored_bit
        self.n_levels = 2 ** self.n_bits

    @torch.jit.export
    def extra_repr(self):
        return 'bit={}, is_training={}, inited={}'.format(
            self.n_bits, self.is_training, self.inited
        )
    
    @classmethod
    def create_quantizer_from_config(cls, config: QuantizerConfig):
        return cls(**config.to_dict())


# =============================================================================
# Adaptive round
# Tailor the original `AdaRoundQuantizer` for adaptive round only
# =============================================================================

class AdaRoundQuantizer(nn.Module):
    """
    Adaptive Rounding Quantizer, used to optimize the rounding policy
    by reconstructing the intermediate output.
    Based on
     Up or Down? Adaptive Rounding for Post-Training Quantization: https://arxiv.org/abs/2004.10568

    :param uaq: WUniformAffineQuantizer, used to initialize quantization parameters in this quantizer
    :param round_mode: controls the forward pass in this quantizer
    :param weight_tensor: initialize alpha
    """

    def __init__(self, uaq: WUniformAffineQuantizer, weight_tensor: torch.Tensor, round_mode='learned_hard_sigmoid'):
        super(AdaRoundQuantizer, self).__init__()
        assert round_mode == "learned_hard_sigmoid", "Only support learned_hard_sigmoid"
        # copying all attributes from WUniformAffineQuantizer
        self.n_bits = uaq.n_bits
        self.sym = uaq.sym
        self.delta = uaq.delta
        self.zero_point = uaq.zero_point
        self.n_levels = uaq.n_levels

        self.round_mode = round_mode
        self.alpha = None
        self.soft_targets = False

        # params for sigmoid function
        self.gamma, self.zeta = -0.1, 1.1
        self.beta = 2/3
        self.init_alpha(x=weight_tensor.clone())

    def forward(self, x):
        # if self.round_mode == 'nearest':
        #     x_int = torch.round(x / self.delta)
        # elif self.round_mode == 'nearest_ste':
        #     x_int = round_ste(x / self.delta)
        # elif self.round_mode == 'stochastic':
        #     x_floor = torch.floor(x / self.delta)
        #     rest = (x / self.delta) - x_floor  # rest of rounding
        #     x_int = x_floor + torch.bernoulli(rest)
        #     print('Draw stochastic sample')
        if self.round_mode == 'learned_hard_sigmoid':
            x_floor = torch.floor(x / self.delta)
            if self.soft_targets:
                x_int = x_floor + self.get_soft_targets()
            else:
                x_int = x_floor + (self.alpha >= 0).float()
        else:
            raise ValueError('Wrong rounding mode')

        x_quant = torch.clamp(x_int, - 2 ** (self.n_bits - 1), 2 ** (self.n_bits - 1) - 1)
        x_float_q = x_quant * self.delta

        return x_float_q

    def get_soft_targets(self):
        return torch.clamp(torch.sigmoid(self.alpha) * (self.zeta - self.gamma) + self.gamma, 0, 1)

    def init_alpha(self, x: torch.Tensor):
        x_floor = torch.floor(x / self.delta)
        if self.round_mode == 'learned_hard_sigmoid':
            print('Init alpha to be FP32')
            rest = (x / self.delta) - x_floor  # rest of rounding [0, 1)
            alpha = -torch.log((self.zeta - self.gamma) / (rest - self.gamma) - 1)  # => sigmoid(alpha) = rest
            self.alpha = nn.Parameter(alpha)
        else:
            raise NotImplementedError

    @torch.jit.export
    def extra_repr(self):
        return 'bit={}'.format(self.n_bits)
    
    @classmethod
    def init_from_tensor(cls, x, config: QuantizerConfig):
        weight_uniform_affine_quantizer = WUniformAffineQuantizer.init_from_tensor(x, config, only_delta=True)
        ada_quantizer = cls(uaq=weight_uniform_affine_quantizer, weight_tensor=x)
        return ada_quantizer

    def get_trainable_params(self):
        return [self.alpha]

