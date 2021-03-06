import torch
import torch.nn as nn
from nflows.distributions import Distribution
from typing import List, Dict, Any, Optional
from nflows.utils import torchutils


class PartitionedMultimodalVAE(nn.Module):
    def __init__(
        self,
        s_prior: Distribution,
        m_priors: List[Distribution],
        s_posterior: Distribution,
        m_posteriors: List[Distribution],
        likelihoods: List[Distribution],
        inputs_encoder: nn.Module,
    ):
        super().__init__()
        self.s_prior = s_prior
        self.m_priors = nn.ModuleList(m_priors)

        self.s_posterior = s_posterior
        self.m_posteriors = nn.ModuleList(m_posteriors)

        self.likelihoods = nn.ModuleList(likelihoods)
        self.inputs_encoder = inputs_encoder

    def log_q_z_x(
        self,
        inputs: List[Optional[torch.Tensor]] = None,
        latent=None,
        context=None,
        num_samples=1,
    ):
        # If inputs not specified (and latent and context specified instead)
        if not inputs:
            return self._log_q_z_x(latent, context)

        # Compute posterior contexts / parameters
        q_context = self.inputs_encoder(inputs)
        m_contexts = q_context["m"]  # [B, Z_m]
        s_context = q_context["s"]  # [B, Z_s]

        # Compute s_posterior
        s_latent, log_q_z_s = self.s_posterior.sample_and_log_prob(
            num_samples, s_context
        )
        s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)  # [B*K, Z]
        log_q_z_s = torchutils.merge_leading_dims(log_q_z_s, num_dims=2)  # [B*K]

        # Compute m_posteriors
        m_latents = []
        log_q_z_ms = torch.zeros_like(log_q_z_s)

        for posterior, context in zip(self.m_posteriors, m_contexts):
            # Account for missing modalities
            if context is None:
                m_latents.append(None)

            else:
                m_latent, log_q_z_m = posterior.sample_and_log_prob(
                    num_samples, context=context
                )
                m_latent = torchutils.merge_leading_dims(m_latent, num_dims=2)
                log_q_z_m = torchutils.merge_leading_dims(log_q_z_m, num_dims=2)

                m_latents.append(m_latent)
                log_q_z_ms += log_q_z_m

        log_prob = log_q_z_s + log_q_z_ms

        # log_prob, sampled latents, posterior context / parameters
        return (
            log_prob,
            {"m": m_latents, "s": s_latent},
            {"m": m_contexts, "s": s_context},
        )

    def _log_q_z_x(self, latent, context):
        # Compute log_q_z_x with latent and context specified
        m_latents = latent["m"]
        s_latent = latent["s"]

        m_contexts = context["m"]
        s_context = context["s"]

        # Compute s_posterior
        log_q_z_s = self.s_posterior.log_prob(s_latent, context=s_context)

        # Compute m_posteriors
        log_q_z_ms = torch.zeros_like(log_q_z_s)

        for posterior, context, latent in zip(self.m_posteriors, m_contexts, m_latents):
            # Account for missing modalities
            if context is None:
                continue

            log_q_z_m = posterior.log_prob(latent, context=context)
            log_q_z_ms += log_q_z_m

        return log_q_z_s + log_q_z_ms

    def log_p_z(self, latents):
        m_latents = latents["m"]
        s_latent = latents["s"]
        batch_size = s_latent.shape[0]

        log_p_z_ms = torch.zeros(batch_size, device=s_latent.device)

        for prior, latent in zip(self.m_priors, m_latents):
            # Account for missing modalities
            if latent is None:
                continue

            log_p_z_ms += prior.log_prob(latent)

        log_p_z_s = self.s_prior.log_prob(s_latent)

        return log_p_z_ms + log_p_z_s

    def log_p_x_z(self, inputs, latents, weights, num_samples=1):
        m_latents = latents["m"]
        s_latent = latents["s"]

        log_prob_list = []

        # Compute likelihood for each modality
        for x, m_latent, likelihood, weight in zip(
            inputs, m_latents, self.likelihoods, weights
        ):
            # Account for missing modalities
            if m_latent is None:
                continue

            x = torchutils.repeat_rows(x, num_reps=num_samples)
            # Each modality is conditioned on m_latent + s_latent
            concat_latent = torch.cat([m_latent, s_latent], dim=-1)
            log_prob_list.append(weight * likelihood.log_prob(x, context=concat_latent))

        return torch.stack(log_prob_list).sum(0)

    def decode(self, latents: Dict[Any, Any], mean: bool) -> List[torch.Tensor]:
        """x ~ p(x|z) for each modality

        Parameters
        ----------
        latents : Dict[Any, Any]
            {"m": m_latents,            "s": s_latent}
            {"m": List[Optional[B, Z]], "s": [B, Z]}
        mean : bool
            Uses the mean of the decoder instead of sampling from it

        Returns
        -------
        List[torch.Tensor]
            List[B, D] of length n_modalities
        """
        samples_list = []
        m_latents = latents["m"]
        s_latent = latents["s"]
        batch_size = s_latent.shape[0]

        # Get samples from each decoder
        for likelihood, prior, m_latent in zip(
            self.likelihoods, self.m_priors, m_latents
        ):
            # If missing m_latent, sample from prior instead
            if m_latent is None:
                m_latent = prior.sample(batch_size)

            # Concat modality-specific and -invariant latents
            concat_latent = torch.cat([m_latent, s_latent], dim=-1)

            if mean:
                samples = likelihood.mean(context=concat_latent)
            else:
                samples = likelihood.sample(num_samples=1, context=concat_latent)
                samples = torchutils.merge_leading_dims(samples, num_dims=2)

            samples_list.append(samples)

        return samples_list

    def encode(
        self, inputs: List[Optional[torch.Tensor]], num_samples: int = None
    ) -> Dict[Any, Any]:
        """Encode into modality-specific and -invariant latent space

        Parameters
        ----------
        inputs : List[Optional[torch.Tensor]]
        num_samples : int, optional

        Returns
        -------
        Dict[Any, Any]
            {"m": m_latents,               "s": s_latent}
            {"m": List[Optional[B, Z]],    "s": [B, Z]}
            {"m": List[Optional[B, K, Z]], "s": [B, K, Z]}
        """
        # Encode into posterior dist parameters
        posterior_context = self.inputs_encoder(inputs)
        m_contexts = posterior_context["m"]
        s_context = posterior_context["s"]

        if num_samples is None:
            # Account for missing modalities
            m_latents = [
                None
                if context is None
                else torchutils.merge_leading_dims(
                    posterior.sample(num_samples=1, context=context), num_dims=2
                )
                for posterior, context in zip(self.m_posteriors, m_contexts)
            ]

            s_latent = self.s_posterior.sample(num_samples=1, context=s_context)
            s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)

        else:
            m_latents = [
                None
                if context is None
                else posterior.sample(num_samples=num_samples, context=context)
                for posterior, context in zip(self.m_posteriors, m_contexts)
            ]

            s_latent = self.s_posterior.sample(
                num_samples=num_samples, context=s_context
            )

        return {"m": m_latents, "s": s_latent}

    def sample(self, num_samples: int, mean=False) -> List[torch.Tensor]:
        """z ~ p(z), x ~ p(x|z)

        Parameters
        ----------
        num_samples : int
        mean : bool, optional
            Uses the mean of the decoder instead of sampling from it, by default False

        Returns
        -------
        List[torch.Tensor]
            List[num_samples, D] of length n_modalities
        """
        m_latents = [prior.sample(num_samples) for prior in self.m_priors]
        s_latent = self.s_prior.sample(num_samples)

        return self.decode({"m": m_latents, "s": s_latent}, mean)

    def reconstruct(
        self, inputs: List[Optional[torch.Tensor]], num_samples: int = None, mean=False
    ) -> List[torch.Tensor]:
        pass

    def cross_reconstruct(
        self, inputs: List[torch.Tensor], num_samples: int = None, mean=False
    ) -> torch.Tensor:
        """
        x -> z_x -> y,
        y -> z_y -> x

        Parameters
        ----------
        inputs : List[torch.Tensor]
            List[B, D]
        num_samples : int, optional
            Number of reconstructions to generate per input
            If None, only one reconstruction is generated per input,
            by default None
        mean : bool, optional
            Uses the mean of the decoder instead of sampling from it, by default False

        Returns
        -------
        torch.Tensor
            [B, D] if num_samples is None,
            [B, K, D] otherwise
        """
        # FIXME Only assuming two modalities
        x, y = inputs

        # FIXME Only works for `num_samples` = None

        # x -> y
        x_latents = self.encode([x, None], num_samples)
        y_recons = self.decode(x_latents, mean)[1]

        # y -> x
        y_latents = self.encode([None, y], num_samples)
        x_recons = self.decode(y_latents, mean)[0]

        return [x_recons, y_recons]
