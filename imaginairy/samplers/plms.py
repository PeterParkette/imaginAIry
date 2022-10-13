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


class PLMSSchedule:
    def __init__(
        self,
        model_num_timesteps,  # 1000?
        model_alphas_cumprod,
        ddim_num_steps,  # prompt.steps?
        ddim_discretize="uniform",
    ):
        device = get_device()
        if model_alphas_cumprod.shape[0] != model_num_timesteps:
            raise ValueError("alphas have to be defined for each timestep")

        self.alphas_cumprod = to_torch(model_alphas_cumprod)
        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.sqrt_alphas_cumprod = to_torch(np.sqrt(model_alphas_cumprod.cpu()))
        self.sqrt_one_minus_alphas_cumprod = to_torch(
            np.sqrt(1.0 - model_alphas_cumprod.cpu())
        )

        self.ddim_timesteps = make_ddim_timesteps(
            ddim_discr_method=ddim_discretize,
            num_ddim_timesteps=ddim_num_steps,
            num_ddpm_timesteps=model_num_timesteps,
        )

        # ddim sampling parameters
        ddim_sigmas, ddim_alphas, ddim_alphas_prev = make_ddim_sampling_parameters(
            alphacums=model_alphas_cumprod.cpu(),
            ddim_timesteps=self.ddim_timesteps,
            eta=0.0,
        )
        self.ddim_sigmas = ddim_sigmas.to(torch.float32).to(torch.device(device))
        self.ddim_alphas = ddim_alphas.to(torch.float32).to(torch.device(device))
        self.ddim_alphas_prev = ddim_alphas_prev
        self.ddim_sqrt_one_minus_alphas = (
            np.sqrt(1.0 - ddim_alphas).to(torch.float32).to(torch.device(device))
        )


class PLMSSampler:
    """
    probabilistic least-mean-squares

    Provenance:
    https://github.com/CompVis/latent-diffusion/commit/f0c4e092c156986e125f48c61a0edd38ba8ad059
    https://arxiv.org/abs/2202.09778
    https://github.com/luping-liu/PNDM
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
        quantize_denoised=False,
        **kwargs,
    ):
        if positive_conditioning.shape[0] != batch_size:
            raise ValueError(
                f"Got {positive_conditioning.shape[0]} conditionings but batch-size is {batch_size}"
            )

        schedule = PLMSSchedule(
            model_num_timesteps=self.model.num_timesteps,
            ddim_num_steps=num_steps,
            model_alphas_cumprod=self.model.alphas_cumprod,
            ddim_discretize="uniform",
        )

        if initial_latent is None:
            initial_latent = torch.randn(shape, device="cpu").to(self.device)

        log_latent(initial_latent, "initial latent")

        timesteps = schedule.ddim_timesteps

        time_range = np.flip(timesteps)
        total_steps = timesteps.shape[0]

        old_eps = []
        noisy_latent = initial_latent

        for i, step in enumerate(tqdm(time_range, total=total_steps)):
            index = total_steps - i - 1
            ts = torch.full((batch_size,), step, device=self.device, dtype=torch.long)
            ts_next = torch.full(
                (batch_size,),
                time_range[min(i + 1, len(time_range) - 1)],
                device=self.device,
                dtype=torch.long,
            )

            if mask is not None:
                assert orig_latent is not None
                img_orig = self.model.q_sample(orig_latent, ts)
                noisy_latent = img_orig * mask + (1.0 - mask) * noisy_latent

            noisy_latent, predicted_latent, noise_pred = self.p_sample_plms(
                noisy_latent=noisy_latent,
                neutral_conditioning=neutral_conditioning,
                positive_conditioning=positive_conditioning,
                guidance_scale=guidance_scale,
                time_encoding=ts,
                schedule=schedule,
                index=index,
                quantize_denoised=quantize_denoised,
                temperature=temperature,
                noise_dropout=noise_dropout,
                old_eps=old_eps,
                t_next=ts_next,
            )
            old_eps.append(noise_pred)
            if len(old_eps) >= 4:
                old_eps.pop(0)

            log_latent(noisy_latent, "noisy_latent")
            log_latent(predicted_latent, "predicted_latent")

        return noisy_latent

    @torch.no_grad()
    def p_sample_plms(
        self,
        noisy_latent,
        neutral_conditioning,
        positive_conditioning,
        guidance_scale,
        time_encoding,
        schedule: PLMSSchedule,
        index,
        repeat_noise=False,
        quantize_denoised=False,
        temperature=1.0,
        noise_dropout=0.0,
        old_eps=None,
        t_next=None,
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

        def get_x_prev_and_pred_x0(e_t, index):
            # select parameters corresponding to the currently considered timestep
            alpha_at_t = torch.full(
                (batch_size, 1, 1, 1), schedule.ddim_alphas[index], device=self.device
            )
            alpha_prev_at_t = torch.full(
                (batch_size, 1, 1, 1),
                schedule.ddim_alphas_prev[index],
                device=self.device,
            )
            sigma_t = torch.full(
                (batch_size, 1, 1, 1), schedule.ddim_sigmas[index], device=self.device
            )
            sqrt_one_minus_at = torch.full(
                (batch_size, 1, 1, 1),
                schedule.ddim_sqrt_one_minus_alphas[index],
                device=self.device,
            )

            # current prediction for x_0
            pred_x0 = (noisy_latent - sqrt_one_minus_at * e_t) / alpha_at_t.sqrt()
            if quantize_denoised:
                pred_x0, _, *_ = self.model.first_stage_model.quantize(pred_x0)
            # direction pointing to x_t
            dir_xt = (1.0 - alpha_prev_at_t - sigma_t**2).sqrt() * e_t
            noise = (
                sigma_t
                * noise_like(noisy_latent.shape, self.device, repeat_noise)
                * temperature
            )
            if noise_dropout > 0.0:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            x_prev = alpha_prev_at_t.sqrt() * pred_x0 + dir_xt + noise
            return x_prev, pred_x0

        if len(old_eps) == 0:
            # Pseudo Improved Euler (2nd order)
            x_prev, pred_x0 = get_x_prev_and_pred_x0(noise_pred, index)
            e_t_next = get_noise_prediction(
                denoise_func=self.model.apply_model,
                noisy_latent=x_prev,
                time_encoding=t_next,
                neutral_conditioning=neutral_conditioning,
                positive_conditioning=positive_conditioning,
                signal_amplification=guidance_scale,
            )
            e_t_prime = (noise_pred + e_t_next) / 2
        elif len(old_eps) == 1:
            # 2nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (3 * noise_pred - old_eps[-1]) / 2
        elif len(old_eps) == 2:
            # 3nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (23 * noise_pred - 16 * old_eps[-1] + 5 * old_eps[-2]) / 12
        elif len(old_eps) >= 3:
            # 4nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (
                55 * noise_pred - 59 * old_eps[-1] + 37 * old_eps[-2] - 9 * old_eps[-3]
            ) / 24

        x_prev, pred_x0 = get_x_prev_and_pred_x0(e_t_prime, index)
        log_latent(x_prev, "x_prev")
        log_latent(pred_x0, "pred_x0")

        return x_prev, pred_x0, noise_pred

    @torch.no_grad()
    def noise_an_image(self, init_latent, t, schedule, noise=None):
        # replace with ddpm.q_sample?
        # fast, but does not allow for exact reconstruction
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
        neutral_conditioning,
        positive_conditioning,
        guidance_scale,
        schedule,
        initial_latent=None,
        t_start=None,
        temperature=1.0,
        mask=None,
        orig_latent=None,
        noise=None,
    ):
        timesteps = schedule.ddim_timesteps[:t_start]

        time_range = np.flip(timesteps)
        total_steps = timesteps.shape[0]

        x_dec = initial_latent
        old_eps = []
        log_latent(x_dec, "x_dec")

        # not sure what the downside of using the same noise throughout the process would be...
        # seems to work fine. maybe it runs faster?
        noise = (
            torch.randn_like(x_dec, device="cpu").to(x_dec.device)
            if noise is None
            else noise
        )
        for i, step in enumerate(tqdm(time_range, total=total_steps)):
            index = total_steps - i - 1
            ts = torch.full(
                (initial_latent.shape[0],),
                step,
                device=initial_latent.device,
                dtype=torch.long,
            )
            ts_next = torch.full(
                (initial_latent.shape[0],),
                time_range[min(i + 1, len(time_range) - 1)],
                device=self.device,
                dtype=torch.long,
            )

            if mask is not None:
                assert orig_latent is not None
                xdec_orig = self.model.q_sample(orig_latent, ts, noise)
                log_latent(xdec_orig, f"xdec_orig i={i} index-{index}")
                # this helps prevent the weird disjointed images that can happen with masking
                hint_strength = 0.8
                if i < 2:
                    xdec_orig_with_hints = (
                        xdec_orig * (1 - hint_strength) + orig_latent * hint_strength
                    )
                else:
                    xdec_orig_with_hints = xdec_orig
                x_dec = xdec_orig_with_hints * mask + (1.0 - mask) * x_dec
                log_latent(x_dec, f"x_dec {ts}")

            x_dec, pred_x0, noise_prediction = self.p_sample_plms(
                noisy_latent=x_dec,
                guidance_scale=guidance_scale,
                neutral_conditioning=neutral_conditioning,
                positive_conditioning=positive_conditioning,
                time_encoding=ts,
                schedule=schedule,
                index=index,
                temperature=temperature,
                old_eps=old_eps,
                t_next=ts_next,
            )

            old_eps.append(noise_prediction)
            if len(old_eps) >= 4:
                old_eps.pop(0)

            log_latent(x_dec, f"x_dec {i}")
            log_latent(pred_x0, f"pred_x0 {i}")
        return x_dec
