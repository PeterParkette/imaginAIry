# pylama:ignore=W0613
import logging

import numpy as np
import torch
from tqdm import tqdm

from imaginairy.log_utils import log_latent
from imaginairy.modules.diffusion.util import (
    extract_into_tensor,
    make_ddim_sampling_parameters,
    make_ddim_timesteps,
    noise_like,
)
from imaginairy.samplers.base import get_noise_prediction
from imaginairy.utils import get_device

logger = logging.getLogger(__name__)


def to_torch(x):
    return x.clone().detach().to(torch.float32).to(get_device())


class DDIMSchedule:
    def __init__(
        self,
        model_num_timesteps,
        model_alphas_cumprod,
        ddim_num_steps,
        ddim_discretize="uniform",
        ddim_eta=0.0,
    ):
        device = get_device()
        if not model_alphas_cumprod.shape[0] == model_num_timesteps:
            raise ValueError("alphas have to be defined for each timestep")

        self.alphas_cumprod = to_torch(model_alphas_cumprod)

        ddim_timesteps = make_ddim_timesteps(
            ddim_discr_method=ddim_discretize,
            num_ddim_timesteps=ddim_num_steps,
            num_ddpm_timesteps=model_num_timesteps,
        )

        # ddim sampling parameters
        ddim_sigmas, ddim_alphas, ddim_alphas_prev = make_ddim_sampling_parameters(
            alphacums=model_alphas_cumprod.cpu(),
            ddim_timesteps=ddim_timesteps,
            eta=ddim_eta,
        )
        self.ddim_timesteps = ddim_timesteps

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.sqrt_alphas_cumprod = to_torch(np.sqrt(model_alphas_cumprod.cpu()))
        self.sqrt_one_minus_alphas_cumprod = to_torch(
            np.sqrt(1.0 - model_alphas_cumprod.cpu())
        )
        self.ddim_sigmas = ddim_sigmas.to(torch.float32).to(device)
        self.ddim_alphas = ddim_alphas.to(torch.float32).to(device)
        self.ddim_alphas_prev = ddim_alphas_prev
        self.ddim_sqrt_one_minus_alphas = (
            np.sqrt(1.0 - ddim_alphas).to(torch.float32).to(device)
        )


class DDIMSampler:
    """
    Denoising Diffusion Implicit Models

    https://arxiv.org/abs/2010.02502
    """

    def __init__(self, model):
        self.model = model
        self.device = get_device()

    @torch.no_grad()
    def sample(
        self,
        num_steps,
        shape,
        neutral_conditioning,
        positive_conditioning,
        guidance_scale=1.0,
        batch_size=1,
        mask=None,
        orig_latent=None,
        temperature=1.0,
        noise_dropout=0.0,
        initial_latent=None,
        quantize_x0=False,
    ):
        if positive_conditioning.shape[0] != batch_size:
            raise ValueError(
                f"Got {positive_conditioning.shape[0]} conditionings but batch-size is {batch_size}"
            )

        schedule = DDIMSchedule(
            model_num_timesteps=self.model.num_timesteps,
            model_alphas_cumprod=self.model.alphas_cumprod,
            ddim_num_steps=num_steps,
            ddim_discretize="uniform",
        )

        if initial_latent is None:
            initial_latent = torch.randn(shape, device="cpu").to(self.device)

        log_latent(initial_latent, "initial latent")

        timesteps = schedule.ddim_timesteps

        time_range = np.flip(timesteps)
        total_steps = timesteps.shape[0]
        noisy_latent = initial_latent

        for i, step in enumerate(tqdm(time_range, total=total_steps)):
            index = total_steps - i - 1
            ts = torch.full((batch_size,), step, device=self.device, dtype=torch.long)

            if mask is not None:
                assert orig_latent is not None
                img_orig = self.model.q_sample(orig_latent, ts)
                noisy_latent = img_orig * mask + (1.0 - mask) * noisy_latent

            noisy_latent, predicted_latent = self.p_sample_ddim(
                noisy_latent=noisy_latent,
                neutral_conditioning=neutral_conditioning,
                positive_conditioning=positive_conditioning,
                guidance_scale=guidance_scale,
                time_encoding=ts,
                index=index,
                schedule=schedule,
                quantize_denoised=quantize_x0,
                temperature=temperature,
                noise_dropout=noise_dropout,
            )

            log_latent(noisy_latent, "noisy_latent")
            log_latent(predicted_latent, "predicted_latent")

        return noisy_latent

    def p_sample_ddim(
        self,
        noisy_latent,
        neutral_conditioning,
        positive_conditioning,
        guidance_scale,
        time_encoding,
        index,
        schedule,
        repeat_noise=False,
        quantize_denoised=False,
        temperature=1.0,
        noise_dropout=0.0,
        loss_function=None,
    ):
        assert guidance_scale >= 1
        noise_pred = get_noise_prediction(
            denoise_func=self.model.apply_model,
            noisy_latent=noisy_latent,
            time_encoding=time_encoding,
            neutral_conditioning=neutral_conditioning,
            positive_conditioning=positive_conditioning,
            signal_amplification=guidance_scale,
        )

        batch_size = noisy_latent.shape[0]

        # select parameters corresponding to the currently considered timestep
        a_t = torch.full(
            (batch_size, 1, 1, 1),
            schedule.ddim_alphas[index],
            device=noisy_latent.device,
        )
        a_prev = torch.full(
            (batch_size, 1, 1, 1),
            schedule.ddim_alphas_prev[index],
            device=noisy_latent.device,
        )
        sigma_t = torch.full(
            (batch_size, 1, 1, 1),
            schedule.ddim_sigmas[index],
            device=noisy_latent.device,
        )
        sqrt_one_minus_at = torch.full(
            (batch_size, 1, 1, 1),
            schedule.ddim_sqrt_one_minus_alphas[index],
            device=noisy_latent.device,
        )
        noisy_latent, predicted_latent = self._p_sample_ddim_formula(
            noisy_latent=noisy_latent,
            noise_pred=noise_pred,
            sqrt_one_minus_at=sqrt_one_minus_at,
            a_t=a_t,
            sigma_t=sigma_t,
            a_prev=a_prev,
            noise_dropout=noise_dropout,
            repeat_noise=repeat_noise,
            temperature=temperature,
        )
        return noisy_latent, predicted_latent

    @staticmethod
    def _p_sample_ddim_formula(
        noisy_latent,
        noise_pred,
        sqrt_one_minus_at,
        a_t,
        sigma_t,
        a_prev,
        noise_dropout,
        repeat_noise,
        temperature,
    ):
        predicted_latent = (noisy_latent - sqrt_one_minus_at * noise_pred) / a_t.sqrt()
        # direction pointing to x_t
        dir_xt = (1.0 - a_prev - sigma_t**2).sqrt() * noise_pred
        noise = (
            sigma_t
            * noise_like(noisy_latent.shape, noisy_latent.device, repeat_noise)
            * temperature
        )
        if noise_dropout > 0.0:
            noise = torch.nn.functional.dropout(noise, p=noise_dropout)
        x_prev = a_prev.sqrt() * predicted_latent + dir_xt + noise
        return x_prev, predicted_latent

    @torch.no_grad()
    def noise_an_image(self, init_latent, t, schedule, noise=None):
        # t serves as an index to gather the correct alphas
        t = t.clamp(0, 1000)
        sqrt_alphas_cumprod = torch.sqrt(schedule.ddim_alphas)
        sqrt_one_minus_alphas_cumprod = schedule.ddim_sqrt_one_minus_alphas

        if noise is None:
            noise = torch.randn_like(init_latent, device="cpu").to(get_device())
        return (
            extract_into_tensor(sqrt_alphas_cumprod, t, init_latent.shape) * init_latent
            + extract_into_tensor(sqrt_one_minus_alphas_cumprod, t, init_latent.shape)
            * noise
        )

    @torch.no_grad()
    def decode(
        self,
        initial_latent,
        neutral_conditioning,
        positive_conditioning,
        guidance_scale,
        t_start,
        schedule,
        temperature=1.0,
        mask=None,
        orig_latent=None,
    ):

        timesteps = schedule.ddim_timesteps[:t_start]

        time_range = np.flip(timesteps)
        total_steps = timesteps.shape[0]

        noisy_latent = initial_latent

        for i, step in enumerate(tqdm(time_range, total=total_steps)):
            index = total_steps - i - 1
            ts = torch.full(
                (initial_latent.shape[0],),
                step,
                device=initial_latent.device,
                dtype=torch.long,
            )

            if mask is not None:
                assert orig_latent is not None
                xdec_orig = self.model.q_sample(orig_latent, ts)
                log_latent(xdec_orig, "xdec_orig")
                # this helps prevent the weird disjointed images that can happen with masking
                hint_strength = 0.8
                if i < 2:
                    xdec_orig_with_hints = (
                        xdec_orig * (1 - hint_strength) + orig_latent * hint_strength
                    )
                else:
                    xdec_orig_with_hints = xdec_orig
                noisy_latent = xdec_orig_with_hints * mask + (1.0 - mask) * noisy_latent
                log_latent(noisy_latent, "noisy_latent")

            noisy_latent, predicted_latent = self.p_sample_ddim(
                noisy_latent=noisy_latent,
                positive_conditioning=positive_conditioning,
                time_encoding=ts,
                schedule=schedule,
                index=index,
                guidance_scale=guidance_scale,
                neutral_conditioning=neutral_conditioning,
                temperature=temperature,
            )

            log_latent(noisy_latent, f"noisy_latent {i}")
            log_latent(predicted_latent, f"predicted_latent {i}")
        return noisy_latent
