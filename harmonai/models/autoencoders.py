import torch
from torch import nn
from torch.nn import functional as F
import numpy as np
from encodec.modules import SEANetEncoder, SEANetDecoder
from typing import Literal, Dict, Any, Callable, Optional

from ..inference.sampling import sample
from .diffusion import create_diffusion_from_config
from .factory import create_pretransform_from_config
from .pretransforms import Pretransform

def vae_sample(mean, scale):
        stdev = nn.functional.softplus(scale) + 1e-4
        var = stdev * stdev
        logvar = torch.log(var)
        latents = torch.randn_like(mean) * stdev + mean

        kl = (mean * mean + var - logvar - 1).sum(1).mean()

        return latents, kl

# Modified from https://github.com/wesbz/SoundStream/blob/main/net.py
class CausalConv1d(nn.Conv1d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.causal_padding = self.dilation[0] * (self.kernel_size[0] - 1)

    def forward(self, x):
        return self._conv_forward(F.pad(x, [self.causal_padding, 0]), self.weight, self.bias)

class CausalConvTranspose1d(nn.ConvTranspose1d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.causal_padding = self.dilation[0] * (self.kernel_size[0] - 1) + self.output_padding[0] + 1 - self.stride[0]
    
    def forward(self, x, output_size=None):
        if self.padding_mode != 'zeros':
            raise ValueError('Only `zeros` padding mode is supported for ConvTranspose1d')

        assert isinstance(self.padding, tuple)
        output_padding = self._output_padding(
            x, output_size, self.stride, self.padding, self.kernel_size, self.dilation)
        return F.conv_transpose1d(
            x, self.weight, self.bias, self.stride, self.padding,
            output_padding, self.groups, self.dilation)[...,:-self.causal_padding]


class ResidualUnit(nn.Module):
    def __init__(self, in_channels, out_channels, dilation):
        super().__init__()
        
        self.dilation = dilation

        self.layers = nn.Sequential(
            CausalConv1d(in_channels=in_channels, out_channels=out_channels,
                      kernel_size=7, dilation=dilation),
            nn.ELU(),
            nn.Conv1d(in_channels=out_channels, out_channels=out_channels,
                      kernel_size=1)
        )

    def forward(self, x):
        return x + self.layers(x)

class EncoderBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride):
        super().__init__()

        self.layers = nn.Sequential(
            ResidualUnit(in_channels=in_channels,
                         out_channels=in_channels, dilation=1),
            nn.ELU(),
            ResidualUnit(in_channels=in_channels,
                         out_channels=in_channels, dilation=3),
            nn.ELU(),
            ResidualUnit(in_channels=in_channels,
                         out_channels=in_channels, dilation=9),
            nn.ELU(),
            ResidualUnit(in_channels=in_channels,
                         out_channels=in_channels, dilation=1),
            nn.ELU(),
            ResidualUnit(in_channels=in_channels,
                         out_channels=in_channels, dilation=3),
            nn.ELU(),
            ResidualUnit(in_channels=in_channels,
                         out_channels=in_channels, dilation=9),
            nn.ELU(),
            CausalConv1d(in_channels=in_channels, out_channels=out_channels,
                      kernel_size=2*stride, stride=stride)
        )

    def forward(self, x):
        return self.layers(x)

class DecoderBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride):
        super().__init__()

        self.layers = nn.Sequential(
            CausalConvTranspose1d(in_channels=in_channels,
                               out_channels=out_channels,
                               kernel_size=2*stride, stride=stride),
            nn.ELU(),
            ResidualUnit(in_channels=out_channels, out_channels=out_channels,
                         dilation=1),
            nn.ELU(),
            ResidualUnit(in_channels=out_channels, out_channels=out_channels,
                         dilation=3),
            nn.ELU(),
            ResidualUnit(in_channels=out_channels, out_channels=out_channels,
                         dilation=9),
            nn.ELU(),
            ResidualUnit(in_channels=out_channels, out_channels=out_channels,
                         dilation=1),
            nn.ELU(),
            ResidualUnit(in_channels=out_channels, out_channels=out_channels,
                         dilation=3),
            nn.ELU(),
            ResidualUnit(in_channels=out_channels, out_channels=out_channels,
                         dilation=9),
        )

    def forward(self, x):
        return self.layers(x)

class AudioEncoder(nn.Module):
    def __init__(self, in_channels=2, channels=64, latent_dim=32, c_mults = [2, 4, 8, 16, 32], strides = [2, 2, 2, 2, 2]):
        super().__init__()
          
        c_mults = [1] + c_mults

        self.depth = len(c_mults)

        layers = [
            CausalConv1d(in_channels=in_channels, out_channels=c_mults[0] * channels, kernel_size=7),
            nn.ELU()
        ]
        
        for i in range(self.depth-1):
            layers.append(EncoderBlock(in_channels=c_mults[i]*channels, out_channels=c_mults[i+1]*channels, stride=strides[i]))
            layers.append(nn.ELU())

        layers.append(CausalConv1d(in_channels=c_mults[-1]*channels, out_channels=latent_dim, kernel_size=3))

        self.layers = nn.Sequential(*layers)

    def forward(self, x):
        return self.layers(x)


class AudioDecoder(nn.Module):
    def __init__(self, out_channels=2, channels=64, latent_dim=32, c_mults = [2, 4, 8, 16, 32], strides = [2, 2, 2, 2, 2]):
        super().__init__()

        c_mults = [1] + c_mults
        
        self.depth = len(c_mults)

        layers = [
            CausalConv1d(in_channels=latent_dim, out_channels=c_mults[-1]*channels, kernel_size=7),
            nn.ELU()
        ]
        
        for i in range(self.depth-1, 0, -1):
            layers.append(DecoderBlock(in_channels=c_mults[i]*channels, out_channels=c_mults[i-1]*channels, stride=strides[i-1]))
            layers.append(nn.ELU())

        layers.append(CausalConv1d(in_channels=c_mults[0] * channels, out_channels=out_channels, kernel_size=7))

        self.layers = nn.Sequential(*layers)

    def forward(self, x):
        return self.layers(x)

class AudioAutoencoder(nn.Module):
    def __init__(
        self,
        encoder,
        decoder,
        latent_dim,
        downsampling_ratio,
        io_channels=2,
        bottleneck: Literal["vae", "tanh", "none"] = "tanh",
        encode_fn: Callable[[torch.Tensor, nn.Module], torch.Tensor] = lambda x, encoder: encoder(x),
        decode_fn: Callable[[torch.Tensor, nn.Module], torch.Tensor] = lambda x, decoder: decoder(x),
        pretransform = None
    ):
        super().__init__()

        self.downsampling_ratio = downsampling_ratio

        self.latent_dim = latent_dim
        self.io_channels = io_channels

        self.bottleneck = bottleneck

        self.encoder = encoder
        self.encode_fn = encode_fn

        self.decoder = decoder
        self.decode_fn = decode_fn

        self.pretransform = pretransform
 
    def encode(self, audio, return_info=False, skip_pretransform=False):

        info = {}

        if self.pretransform is not None and not skip_pretransform:
            with torch.no_grad():
                audio = self.pretransform.encode(audio)

        if self.bottleneck == "vae":
            mean, scale = self.encode_fn(audio, self.encoder).chunk(2, dim=1)
            latents, kl = vae_sample(mean, scale)
            info['kl'] = kl
        else:
            latents = self.encode_fn(audio, self.encoder)
            
            if self.bottleneck == "tanh":
                latents = torch.tanh(latents)
            elif self.bottleneck == "l2_norm":
                latents = F.normalize(latents, dim=1)

        if return_info:
            return latents, info

        return latents

    def decode(self, latents, **kwargs):

        if self.bottleneck == "l2_norm":
            latents = F.normalize(latents, dim=1)

        decoded = self.decode_fn(latents, self.decoder, **kwargs)

        if self.pretransform is not None:
            with torch.no_grad():
                decoded = self.pretransform.decode(decoded)
        
        return decoded
    
class DiffusionAutoencoder(AudioAutoencoder):
    def __init__(
        self,
        diffusion,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)

        self.diffusion = diffusion

        # Shrink the initial encoder parameters to avoid saturated latents
        with torch.no_grad():
            for param in self.encoder.parameters():
                param *= 0.5

    def decode(self, latents, steps=100):
        
        upsampled_length = latents.shape[2] * self.downsampling_ratio

        if self.bottleneck == "l2_norm":
            latents = F.normalize(latents, dim=1)

        if self.decoder:
            latents = self.decode_fn(latents, self.decoder)

        noise = torch.randn(latents.shape[0], self.io_channels, upsampled_length, device=latents.device)
        decoded = sample(self.diffusion, noise, steps, 0, cond=latents)

        if self.pretransform is not None:
            with torch.no_grad():
                decoded = self.pretransform.decode(decoded)

        return decoded
        
# AE factories

def create_encoder_from_config(encoder_config: Dict[str, Any]):
    encoder_type = encoder_config.get("type", None)
    assert encoder_type is not None, "Encoder type must be specified"

    if encoder_type == "audio_ae":
        return AudioEncoder(
            **encoder_config["config"]
        )
    
    elif encoder_type == "seanet":
        seanet_encoder_config = encoder_config["config"]

        #SEANet encoder expects strides in reverse order
        seanet_encoder_config["ratios"] = list(reversed(seanet_encoder_config.get("ratios", [2, 2, 2, 2, 2])))
        return SEANetEncoder(
            **seanet_encoder_config
        )
    
    else:
        raise ValueError(f"Unknown encoder type {encoder_type}")

def create_decoder_from_config(decoder_config: Dict[str, Any]):
    decoder_type = decoder_config.get("type", None)
    assert decoder_type is not None, "Decoder type must be specified"

    if decoder_type == "audio_ae":
        return AudioDecoder(
            **decoder_config["config"]
        )
    elif decoder_type == "seanet":
        return SEANetDecoder(
            **decoder_config["config"]
        )
    else:
        raise ValueError(f"Unknown decoder type {decoder_type}")

def create_autoencoder_from_config(model_config: Dict[str, Any]):
    
    encoder = create_encoder_from_config(model_config["encoder"])
    decoder = create_decoder_from_config(model_config["decoder"])

    bottleneck = model_config.get("bottleneck", "tanh")

    latent_dim = model_config.get("latent_dim", None)
    assert latent_dim is not None, "latent_dim must be specified in model config"
    downsampling_ratio = model_config.get("downsampling_ratio", None)
    assert downsampling_ratio is not None, "downsampling_ratio must be specified in model config"
    io_channels = model_config.get("io_channels", None)
    assert io_channels is not None, "io_channels must be specified in model config"

    pretransform = model_config.get("pretransform", None)

    if pretransform is not None:
        pretransform = create_pretransform_from_config(pretransform)
    
    return AudioAutoencoder(
        encoder,
        decoder,
        io_channels=io_channels,
        latent_dim=latent_dim,
        downsampling_ratio=downsampling_ratio,
        bottleneck=bottleneck,
        pretransform=pretransform
    )

def create_diffAE_from_config(model_config: Dict[str, Any]):
    
    encoder = create_encoder_from_config(model_config["encoder"])

    decoder = create_decoder_from_config(model_config["decoder"])

    diffusion = create_diffusion_from_config(model_config["diffusion"])

    latent_dim = model_config.get("latent_dim", None)
    assert latent_dim is not None, "latent_dim must be specified in model config"
    downsampling_ratio = model_config.get("downsampling_ratio", None)
    assert downsampling_ratio is not None, "downsampling_ratio must be specified in model config"
    io_channels = model_config.get("io_channels", None)
    assert io_channels is not None, "io_channels must be specified in model config"

    bottleneck = model_config.get("bottleneck", None)

    pretransform = model_config.get("pretransform", None)

    if pretransform is not None:
        pretransform = create_pretransform_from_config(pretransform)

    return DiffusionAutoencoder(
        encoder=encoder,
        decoder=decoder,
        diffusion=diffusion,
        io_channels=io_channels,
        latent_dim=latent_dim,
        downsampling_ratio=downsampling_ratio,
        bottleneck=bottleneck,
        pretransform=pretransform
    )