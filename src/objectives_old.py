"""Most objectives require:

Reconstruction Term
- Samples from q(z|x)
- log p(x|z)

KL Term
- KL[q(z|x) | p(z)]
- log q(z|x)
- log p(z)

KL Annealing?

Pathwise derivative
Dreg
"""
from typing import List, Union, Optional, Any
import torch
import torch.nn as nn
from src.utils import set_default_tensor_type
from nflows.utils import torchutils


# FIXME Test this function!!
# FIXME Any way to vectorize this?
@set_default_tensor_type(torch.cuda.FloatTensor)
def vaevae_elbo(
    model,
    inputs: List[torch.Tensor],
    likelihood_weights: List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    # FIXME Get rid of num_samples?
    # FIXME Add kl and likelihood weights?
    # Compute kl analytically?

    x, y = inputs
    x_likelihood, y_likelihood = model.likelihood

    # Compute unimodal components
    elbo_sum, contexts = unimodal_elbos(
        model,
        inputs,
        likelihood_weights,
        num_samples=num_samples,
        kl_multiplier=kl_multiplier,
    )
    # Parameters for q(z|x) and q(z|y)
    x_context, y_context = contexts
    x_weight, y_weight = likelihood_weights

    # Compute bimodal components
    xy_context = model.inputs_encoder(inputs)

    z, log_q_z_xy = model.approximate_posterior.sample_and_log_prob(
        num_samples, context=xy_context
    )
    z = torchutils.merge_leading_dims(z, num_dims=2)
    log_q_z_xy = torchutils.merge_leading_dims(log_q_z_xy, num_dims=2)

    # HACK hardcode
    log_q_z_x = model.approximate_posterior.log_prob(z, context=x_context)
    log_q_z_y = model.approximate_posterior.log_prob(z, context=y_context)

    kl_1 = log_q_z_xy - log_q_z_x
    kl_2 = log_q_z_xy - log_q_z_y

    x = torchutils.repeat_rows(x, num_reps=num_samples)
    y = torchutils.repeat_rows(y, num_reps=num_samples)
    log_p_x_z = x_likelihood.log_prob(x, context=z)
    log_p_x_y = y_likelihood.log_prob(y, context=z)

    # FIXME Minus or plus KL??
    # FIXME How to weigh these terms?
    elbo = (
        (x_weight * log_p_x_z)
        + (y_weight * log_p_x_y)
        - (kl_multiplier * kl_1)
        - (kl_multiplier * kl_2)
    )
    elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])
    elbo = elbo.mean(dim=1)  # Average across number of samples

    elbo_sum += elbo

    return elbo_sum


@set_default_tensor_type(torch.cuda.FloatTensor)
def mvae_elbo(
    model,
    inputs: List[torch.Tensor],
    likelihood_weights: List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    """ELBO(x, y) + ELBO(x) + ELBO(y)"""

    # Marginal ELBOs
    elbo_sum, _ = unimodal_elbos(
        model,
        inputs,
        likelihood_weights,
        num_samples=num_samples,
        kl_multiplier=kl_multiplier,
    )

    # Joint ELBO
    elbo_sum += stochastic_elbo(
        model,
        inputs,
        num_samples=num_samples,
        kl_multiplier=kl_multiplier,
        likelihood_weight=likelihood_weights,
    )

    return elbo_sum


@set_default_tensor_type(torch.cuda.FloatTensor)
def stochastic_elbo(
    model: nn.Module,
    inputs: Union[torch.Tensor, List[torch.Tensor]],
    num_samples=1,
    kl_multiplier=1.0,
    likelihood_weight: Union[float, List[float]] = 1.0,
    keepdim=False,
) -> torch.Tensor:
    """Calculates an unbiased Monte-Carlo estimate of the evidence lower bound.
    Supports multimodal inputs.

    Note: the KL term is also estimated via Monte Carlo

    Parameters
    ----------
    model : nn.Module
        VAE model
    inputs : torch.Tensor | List[torch.Tensor]
        [B, D], Supports multimodal inputs
    num_samples : int, optional
        Number of samples to use for the Monte-Carlo estimate, by default 1
    kl_multiplier : float, optional
        , by default 1.0
    likelihood_weight: float | List[float], optional
        How much to weigh the reconstruction term, by default 1.0
        Supports multimodal inputs
    keepdim : bool, optional
        , by default False

    Returns
    -------
    torch.Tensor
        An ELBO estimate for each input
        [B, K] if keepdim
        [B] otherwise
    """
    # Sample latents and calculate their log prob under the encoder
    if model.inputs_encoder is None:
        posterior_context = inputs
    else:
        posterior_context = model.inputs_encoder(inputs)

    # Compute log prob of latents under the posterior
    latents, log_q_z = model.approximate_posterior.sample_and_log_prob(
        num_samples, context=posterior_context
    )
    latents = torchutils.merge_leading_dims(latents, num_dims=2)
    log_q_z = torchutils.merge_leading_dims(log_q_z, num_dims=2)

    # Compute log prob of latents under the prior
    log_p_z = model.prior.log_prob(latents)

    # Compute log prob of inputs under the decoder
    # If inputs are multimodal
    if isinstance(inputs, List):
        if likelihood_weight == 1.0:
            likelihood_weight = [1.0] * len(inputs)

        # Compute log prob of inputs under each decoder
        log_p_x = torch.zeros_like(log_p_z)

        for x, l, w in zip(inputs, model.likelihood, likelihood_weight):
            x = torchutils.repeat_rows(x, num_reps=num_samples)
            log_p_x += w * l.log_prob(x, context=latents)
    else:
        inputs = torchutils.repeat_rows(inputs, num_reps=num_samples)
        log_p_x = likelihood_weight * model.likelihood.log_prob(inputs, context=latents)

    # Compute ELBO
    elbo = log_p_x + (kl_multiplier * (log_p_z - log_q_z))
    elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])

    if keepdim:
        return elbo
    else:
        return torch.sum(elbo, dim=1) / num_samples  # Average ELBO across samples


def unimodal_elbos(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:

    batch_size = inputs[0].shape[0]

    # Compute the ELBO for each modality
    elbo_sum = torch.zeros(batch_size, device=inputs[0].device)
    # Cache the posterior context of each modality
    contexts = []

    for i, (x, likelihood, weight) in enumerate(
        zip(inputs, model.likelihood, likelihood_weights)
    ):
        unimodal_inputs = [None] * len(inputs)
        unimodal_inputs[i] = x

        posterior_context = model.inputs_encoder(unimodal_inputs)

        # Compute log prob of latents under the posterior
        latents, log_q_z = model.approximate_posterior.sample_and_log_prob(
            num_samples, context=posterior_context
        )
        latents = torchutils.merge_leading_dims(latents, num_dims=2)
        log_q_z = torchutils.merge_leading_dims(log_q_z, num_dims=2)

        # Compute log prob of latents under the prior
        log_p_z = model.prior.log_prob(latents)

        # Compute log prob of inputs under the decoder
        x = torchutils.repeat_rows(x, num_reps=num_samples)
        log_p_x = likelihood.log_prob(x, context=latents)

        # Compute ELBO
        elbo = (weight * log_p_x) + (kl_multiplier * (log_p_z - log_q_z))
        elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])

        # Average across number of samples
        elbo_sum += elbo.mean(dim=1)
        contexts.append(posterior_context)

    return elbo_sum, contexts


# PARTITIONED ##################################################################


def partitioned_unimodal_elbos(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    num_samples = int(num_samples)

    batch_size = inputs[0].shape[0]

    # Compute the ELBO for each modality
    elbo_sum = torch.zeros(batch_size, device=inputs[0].device)
    # Cache the posterior context of each modality
    s_contexts = []
    # Cache the sampled modality-specific latents
    m_latents = []

    for i, (x, likelihood, weight, m_prior, m_posterior) in enumerate(
        zip(
            inputs,
            model.likelihoods,
            likelihood_weights,
            model.m_priors,
            model.m_posteriors,
        )
    ):
        unimodal_inputs = [None] * len(inputs)
        unimodal_inputs[i] = x

        posterior_context = model.inputs_encoder(unimodal_inputs)
        m_context = posterior_context["m"][i]
        s_context = posterior_context["s"]

        # Compute log prob of latents under the posterior
        m_latent, log_q_z_m = m_posterior.sample_and_log_prob(
            num_samples, context=m_context
        )
        m_latent = torchutils.merge_leading_dims(m_latent, num_dims=2)
        log_q_z_m = torchutils.merge_leading_dims(log_q_z_m, num_dims=2)

        s_latent, log_q_z_s = model.s_posterior.sample_and_log_prob(
            num_samples, context=s_context
        )
        s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
        log_q_z_s = torchutils.merge_leading_dims(log_q_z_s, num_dims=2)

        # Compute log prob of latents under the prior
        log_p_z_m = m_prior.log_prob(m_latent)
        log_p_z_s = model.s_prior.log_prob(s_latent)

        # Compute log prob of inputs under the decoder
        concat_latent = torch.cat([m_latent, s_latent], dim=-1)
        log_p_x = likelihood.log_prob(x, context=concat_latent)

        # Compute ELBO
        elbo = (weight * log_p_x) + (
            kl_multiplier * (log_p_z_m + log_p_z_s - log_q_z_m - log_q_z_s)
        )

        elbo_sum += elbo
        s_contexts.append(s_context)
        m_latents.append(m_latent)

    return elbo_sum, s_contexts, m_latents


def pmvaevae_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    """PMVAE modified objective"""
    num_samples = int(num_samples)

    x, y = inputs
    x_likelihood, y_likelihood = model.likelihoods

    # Compute unimodal components
    elbo_sum, s_contexts, m_latents = partitioned_unimodal_elbos(
        model,
        inputs,
        likelihood_weights,
        num_samples=num_samples,
        kl_multiplier=kl_multiplier,
    )
    # Parameters for q(z_s|x) and q(z_s|y)
    x_s_context, y_s_context = s_contexts
    x_weight, y_weight = likelihood_weights

    # Compute bimodal components
    xy_s_context = model.inputs_encoder(inputs)["s"]

    s_latent, log_q_z_xy = model.s_posterior.sample_and_log_prob(
        num_samples, context=xy_s_context
    )
    s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
    log_q_z_xy = torchutils.merge_leading_dims(log_q_z_xy, num_dims=2)

    # HACK hardcode
    log_q_z_x = model.s_posterior.log_prob(s_latent, context=x_s_context)
    log_q_z_y = model.s_posterior.log_prob(s_latent, context=y_s_context)

    kl_1 = log_q_z_xy - log_q_z_x
    kl_2 = log_q_z_xy - log_q_z_y

    log_p_x_z = x_likelihood.log_prob(
        x, context=torch.cat([m_latents[0], s_latent], dim=-1)
    )
    log_p_x_y = y_likelihood.log_prob(
        y, context=torch.cat([m_latents[1], s_latent], dim=-1)
    )

    elbo = (
        (x_weight * log_p_x_z)
        + (y_weight * log_p_x_y)
        - (kl_multiplier * kl_1)
        - (kl_multiplier * kl_2)
    )

    elbo_sum += elbo

    return elbo_sum


def pmvae_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    num_samples=1,
    kl_multiplier=1.0,
    keepdim=False,
):
    """PMVAE ELBO"""
    posterior_context = model.inputs_encoder(inputs)
    m_contexts = posterior_context["m"]
    s_context = posterior_context["s"]

    # Compute log prob of latents under the posterior
    m_latents = []
    log_q_z_ms = []

    for posterior, context in zip(model.m_posteriors, m_contexts):
        m_latent, log_q_z_m = posterior.sample_and_log_prob(
            num_samples, context=context
        )
        m_latent = torchutils.merge_leading_dims(m_latent, num_dims=2)
        log_q_z_m = torchutils.merge_leading_dims(log_q_z_m, num_dims=2)

        m_latents.append(m_latent)
        log_q_z_ms.append(log_q_z_m)

    s_latent, log_q_z_s = model.s_posterior.sample_and_log_prob(
        num_samples, context=s_context
    )
    s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
    log_q_z_s = torchutils.merge_leading_dims(log_q_z_s, num_dims=2)

    # Compute log prob of latents under the prior
    log_p_z_ms = [
        prior.log_prob(latent) for prior, latent in zip(model.m_priors, m_latents)
    ]
    log_p_z_s = model.s_prior.log_prob(s_latent)

    # Compute log prob of inputs under the decoder
    log_p_x = torch.zeros_like(log_p_z_s)

    for x, m_latent, likelihood in zip(inputs, m_latents, model.likelihoods):
        x = torchutils.repeat_rows(x, num_reps=num_samples)
        concat_latent = torch.cat([m_latent, s_latent], dim=-1)
        log_p_x += likelihood.log_prob(x, context=concat_latent)

    # Compute ELBO
    log_p_z = torch.stack(log_p_z_ms).sum(0) + log_p_z_s
    log_q_z = torch.stack(log_q_z_ms).sum(0) + log_q_z_s
    elbo = log_p_x + (kl_multiplier * (log_p_z - log_q_z))
    elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])

    if keepdim:
        return elbo
    else:
        return torch.sum(elbo, dim=1) / num_samples  # Average ELBO across samples


# HIERARCHICAL PARTITIONED ######################################################


def hier_partitioned_unimodal_elbos(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    num_samples = int(num_samples)

    batch_size = inputs[0].shape[0]

    # Compute the ELBO for each modality
    elbo_sum = torch.zeros(batch_size, device=inputs[0].device)
    # Cache the posterior context of each modality
    s_contexts = []
    # Cache the sampled modality-specific latents
    m_latents = []

    for i, (x, likelihood, weight, m_prior, m_posterior) in enumerate(
        zip(
            inputs,
            model.likelihoods,
            likelihood_weights,
            model.m_priors,
            model.m_posteriors,
        )
    ):
        unimodal_inputs = [None] * len(inputs)
        unimodal_inputs[i] = x

        posterior_context = model.inputs_encoder(unimodal_inputs)
        m_context = posterior_context["m"][i]
        s_context = posterior_context["s"]

        # Compute log prob of latents under the posterior
        m_latent, log_q_z_m = m_posterior.sample_and_log_prob(
            num_samples, context=m_context
        )
        m_latent = torchutils.merge_leading_dims(m_latent, num_dims=2)
        log_q_z_m = torchutils.merge_leading_dims(log_q_z_m, num_dims=2)

        s_latent, log_q_z_s = model.s_posterior.sample_and_log_prob(
            num_samples, context=s_context
        )
        s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
        log_q_z_s = torchutils.merge_leading_dims(log_q_z_s, num_dims=2)

        # Compute log prob of latents under the prior
        # Condition on s_latent
        log_p_z_m = m_prior.log_prob(m_latent, context=s_latent)
        log_p_z_s = model.s_prior.log_prob(s_latent)

        # Compute log prob of inputs under the decoder
        concat_latent = torch.cat([m_latent, s_latent], dim=-1)
        log_p_x = likelihood.log_prob(x, context=concat_latent)

        # Compute ELBO
        elbo = (weight * log_p_x) + (
            kl_multiplier * (log_p_z_m + log_p_z_s - log_q_z_m - log_q_z_s)
        )

        elbo_sum += elbo
        s_contexts.append(s_context)
        m_latents.append(m_latent)

    return elbo_sum, s_contexts, m_latents


def hier_pmvaevae_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    """PMVAE modified objective"""
    num_samples = int(num_samples)

    x, y = inputs
    x_likelihood, y_likelihood = model.likelihoods

    # Compute unimodal components
    elbo_sum, s_contexts, m_latents = hier_partitioned_unimodal_elbos(
        model,
        inputs,
        likelihood_weights,
        num_samples=num_samples,
        kl_multiplier=kl_multiplier,
    )

    # Parameters for q(z_s|x) and q(z_s|y)
    x_s_context, y_s_context = s_contexts
    x_weight, y_weight = likelihood_weights

    # Compute bimodal components
    xy_s_context = model.inputs_encoder(inputs)["s"]

    s_latent, log_q_z_xy = model.s_posterior.sample_and_log_prob(
        num_samples, context=xy_s_context
    )
    s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
    log_q_z_xy = torchutils.merge_leading_dims(log_q_z_xy, num_dims=2)

    # HACK hardcode
    log_q_z_x = model.s_posterior.log_prob(s_latent, context=x_s_context)
    log_q_z_y = model.s_posterior.log_prob(s_latent, context=y_s_context)

    kl_1 = log_q_z_xy - log_q_z_x
    kl_2 = log_q_z_xy - log_q_z_y

    log_p_x_z = x_likelihood.log_prob(
        x, context=torch.cat([m_latents[0], s_latent], dim=-1)
    )
    log_p_x_y = y_likelihood.log_prob(
        y, context=torch.cat([m_latents[1], s_latent], dim=-1)
    )

    elbo = (
        (x_weight * log_p_x_z)
        + (y_weight * log_p_x_y)
        - (kl_multiplier * kl_1)
        - (kl_multiplier * kl_2)
    )

    elbo_sum += elbo

    return elbo_sum


def mvae_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    kl_multiplier=1.0,
) -> torch.Tensor:
    # To collate all elbo terms
    elbo_list = []

    # Compute unimodal / marginal elbos
    for i, x in enumerate(inputs):
        # Create input list (containing only one modality)
        xs = [None] * len(inputs)
        xs[i] = x

        elbo, _ = compute_elbo(
            model,
            xs,
            likelihood_weights=likelihood_weights,
            kl_multiplier=kl_multiplier,
        )

        elbo_list.append(elbo)

    # Compute multimodal / joint elbo
    joint_elbo, _ = compute_elbo(
        model,
        inputs,
        likelihood_weights=likelihood_weights,
        kl_multiplier=kl_multiplier,
    )
    elbo_list.append(joint_elbo)

    # Sum up all elbo terms
    return torch.stack(elbo_list).sum(0)


def vaevae_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    kl_multiplier=1.0,
) -> torch.Tensor:
    # To collate all elbo terms
    elbo_list = []
    # To cache unimodal posterior parameters (for computing multimodal terms)
    unimodal_q_contexts = []

    # Compute unimodal elbos
    for i, x in enumerate(inputs):
        # Create input list (containing only one modality)
        xs = [None] * len(inputs)
        xs[i] = x

        elbo, q_context = compute_elbo(
            model,
            xs,
            likelihood_weights=likelihood_weights,
            kl_multiplier=kl_multiplier,
        )

        elbo_list.append(elbo)
        unimodal_q_contexts.append(q_context)

    # Compute multimodal elbo terms
    elbo_list.append(
        compute_multimodal_elbo(
            model,
            inputs,
            unimodal_q_contexts,
            likelihood_weights=likelihood_weights,
            kl_multiplier=kl_multiplier,
        )
    )

    # Sum up all elbo terms
    return torch.stack(elbo_list).sum(0)


def compute_multimodal_elbo(
    model: nn.Module,
    inputs: List[Optional[torch.Tensor]],
    unimodal_q_contexts: List[Any],
    likelihood_weights=None,
    kl_multiplier=1.0,
):
    # Multimodal reconstruction term
    # + multimodal <-> unimodal posterior regularization terms
    num_samples = 1

    # Compute log prob of latents under the multimodal posterior
    log_q_z_multi, latents, _ = model.log_q_z_x(inputs, num_samples=num_samples)

    elbo = torch.zeros_like(log_q_z_multi)

    # Compute log prob of latents under the unimodal posteriors
    for q_context in unimodal_q_contexts:
        log_q_z_uni = model.log_q_z_x(latent=latents, context=q_context)
        # Compute multimodal <-> unimodal posterior regularization term
        kl = log_q_z_multi - log_q_z_uni

        elbo -= kl_multiplier * kl

    # Compute likelihood term for all modalities
    # Weight for each likelihood term
    weights = likelihood_weights if likelihood_weights else [1.0] * len(inputs)
    log_p_x_z = model.log_p_x_z(inputs, latents, weights, num_samples=num_samples)

    elbo += log_p_x_z

    return elbo


def compute_elbo(
    model: nn.Module,
    inputs: List[Optional[torch.Tensor]],
    likelihood_weights=None,
    num_samples=1,
    kl_multiplier=1.0,
    keepdim=False,
):
    # Compute log prob of latents under the posterior
    log_q_z_x, latents, q_context = model.log_q_z_x(inputs, num_samples=num_samples)

    # Compute log prob of latents under the prior
    log_p_z = model.log_p_z(latents)

    # Compute log prob of inputs under the decoder
    # Weight for each likelihood term
    weights = likelihood_weights if likelihood_weights else [1.0] * len(inputs)
    log_p_x_z = model.log_p_x_z(inputs, latents, weights, num_samples=num_samples)

    # Compute ELBO
    elbo = log_p_x_z + (kl_multiplier * (log_p_z - log_q_z_x))
    elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])
    if not keepdim:
        elbo = elbo.mean(1)  # Average ELBO across samples

    return elbo, q_context


def stochastic_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    num_samples=1,
    keepdim=False,
):
    elbo, _ = compute_elbo(model, inputs, num_samples=num_samples, keepdim=keepdim)

    return elbo


# HIERARCHICAL PARTITIONED v2 ######################################################


def hier_partitioned_v2_unimodal_elbos(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    num_samples = int(num_samples)

    batch_size = inputs[0].shape[0]

    # Compute the ELBO for each modality
    elbo_sum = torch.zeros(batch_size, device=inputs[0].device)
    # Cache the posterior context of each modality
    s_contexts = []
    # Cache the sampled modality-specific latents
    m_latents = []

    for i, (x, likelihood, weight, m_prior, m_posterior) in enumerate(
        zip(
            inputs,
            model.likelihoods,
            likelihood_weights,
            model.m_priors,
            model.m_posteriors,
        )
    ):
        unimodal_inputs = [None] * len(inputs)
        unimodal_inputs[i] = x

        posterior_context = model.inputs_encoder(unimodal_inputs)
        m_context = posterior_context["m"][i]
        s_context = posterior_context["s"]

        # Compute log prob of latents under the posterior
        m_latent, log_q_z_m = m_posterior.sample_and_log_prob(
            num_samples, context=m_context
        )
        m_latent = torchutils.merge_leading_dims(m_latent, num_dims=2)
        log_q_z_m = torchutils.merge_leading_dims(log_q_z_m, num_dims=2)

        s_latent, log_q_z_s = model.s_posterior.sample_and_log_prob(
            num_samples, context=s_context
        )
        s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
        log_q_z_s = torchutils.merge_leading_dims(log_q_z_s, num_dims=2)

        # Compute log prob of latents under the prior
        # Condition on s_latent
        log_p_z_m = m_prior.log_prob(m_latent, context=s_latent)
        log_p_z_s = model.s_prior.log_prob(s_latent)

        # Compute log prob of inputs under the decoder
        # No need to concat
        log_p_x = likelihood.log_prob(x, context=m_latent)

        # Compute ELBO
        elbo = (weight * log_p_x) + (
            kl_multiplier * (log_p_z_m + log_p_z_s - log_q_z_m - log_q_z_s)
        )

        elbo_sum += elbo
        s_contexts.append(s_context)
        m_latents.append(m_latent)

    return elbo_sum, s_contexts, m_latents


def hier_pmvaevae_v2_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    likelihood_weights=List[float],
    num_samples=1,
    kl_multiplier=1.0,
) -> torch.Tensor:
    """PMVAE modified objective"""
    num_samples = int(num_samples)

    x, y = inputs
    x_likelihood, y_likelihood = model.likelihoods

    # Compute unimodal components
    elbo_sum, s_contexts, m_latents = hier_partitioned_v2_unimodal_elbos(
        model,
        inputs,
        likelihood_weights,
        num_samples=num_samples,
        kl_multiplier=kl_multiplier,
    )

    # Parameters for q(z_s|x) and q(z_s|y)
    x_s_context, y_s_context = s_contexts
    x_weight, y_weight = likelihood_weights

    # Compute bimodal components
    xy_s_context = model.inputs_encoder(inputs)["s"]

    s_latent, log_q_z_xy = model.s_posterior.sample_and_log_prob(
        num_samples, context=xy_s_context
    )
    s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
    log_q_z_xy = torchutils.merge_leading_dims(log_q_z_xy, num_dims=2)

    # HACK hardcode
    log_q_z_x = model.s_posterior.log_prob(s_latent, context=x_s_context)
    log_q_z_y = model.s_posterior.log_prob(s_latent, context=y_s_context)

    kl_1 = log_q_z_xy - log_q_z_x
    kl_2 = log_q_z_xy - log_q_z_y

    # FIXME Extra log p(x) terms?
    # Same m_latent used to generate unimodal terms
    log_p_x_z = x_likelihood.log_prob(x, context=m_latents[0])
    log_p_x_y = y_likelihood.log_prob(y, context=m_latents[1])

    elbo = (
        (x_weight * log_p_x_z)
        + (y_weight * log_p_x_y)
        - (kl_multiplier * kl_1)
        - (kl_multiplier * kl_2)
    )

    elbo_sum += elbo

    return elbo_sum


def hier_pmvae_v2_elbo(
    model: nn.Module,
    inputs: List[torch.Tensor],
    num_samples=1,
    kl_multiplier=1.0,
    keepdim=False,
):
    posterior_context = model.inputs_encoder(inputs)
    m_contexts = posterior_context["m"]
    s_context = posterior_context["s"]

    # Compute log prob of latents under the posterior
    m_latents = []
    log_q_z_ms = []

    for posterior, context in zip(model.m_posteriors, m_contexts):
        m_latent, log_q_z_m = posterior.sample_and_log_prob(
            num_samples, context=context
        )
        m_latent = torchutils.merge_leading_dims(m_latent, num_dims=2)
        log_q_z_m = torchutils.merge_leading_dims(log_q_z_m, num_dims=2)

        m_latents.append(m_latent)
        log_q_z_ms.append(log_q_z_m)

    s_latent, log_q_z_s = model.s_posterior.sample_and_log_prob(
        num_samples, context=s_context
    )
    s_latent = torchutils.merge_leading_dims(s_latent, num_dims=2)
    log_q_z_s = torchutils.merge_leading_dims(log_q_z_s, num_dims=2)

    # Compute log prob of latents under the prior
    # Condition on s_latent
    log_p_z_ms = [
        prior.log_prob(latent, context=s_latent)
        for prior, latent in zip(model.m_priors, m_latents)
    ]
    log_p_z_s = model.s_prior.log_prob(s_latent)

    # Compute log prob of inputs under the decoder
    log_p_x = torch.zeros_like(log_p_z_s)

    for x, m_latent, likelihood in zip(inputs, m_latents, model.likelihoods):
        x = torchutils.repeat_rows(x, num_reps=num_samples)
        # Don't concat
        log_p_x += likelihood.log_prob(x, context=m_latent)

    # Compute ELBO
    log_p_z = torch.stack(log_p_z_ms).sum(0) + log_p_z_s
    log_q_z = torch.stack(log_q_z_ms).sum(0) + log_q_z_s
    elbo = log_p_x + (kl_multiplier * (log_p_z - log_q_z))
    elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])

    if keepdim:
        return elbo
    else:
        return torch.sum(elbo, dim=1) / num_samples  # Average ELBO across samples


# MISC #########################################################################


@set_default_tensor_type(torch.cuda.FloatTensor)
def log_prob_lower_bound(model, inputs: torch.Tensor, num_samples=100) -> torch.Tensor:
    # FIXME change stochastic_elbo to something else?
    elbo = stochastic_elbo(model, inputs, num_samples=num_samples, keepdim=True)
    log_prob_lower_bound = torch.logsumexp(elbo, dim=1) - torch.log(
        torch.Tensor([num_samples])
    )

    return log_prob_lower_bound


# @set_default_tensor_type(torch.cuda.FloatTensor)
# def path_derivative_elbo(
#     model, inputs: torch.Tensor, num_samples=1, kl_multiplier=1, keepdim=False
# ):
#     # Sample latents and calculate their log prob under the encoder

#     # Get posterior mean and std parameters
#     if model.inputs_encoder is None:
#         posterior_context = inputs
#     else:
#         posterior_context = model.inputs_encoder(inputs)

#     latents = model.approximate_posterior.sample(num_samples, context=posterior_context)
#     latents = torchutils.merge_leading_dims(latents, num_dims=2)

#     # Stop gradient on approx posterior parameters
#     posterior_context_sg = posterior_context.detach()
#     log_q_z = model.approximate_posterior.log_prob(
#         latents, context=posterior_context_sg
#     )

#     # log_q_z = torchutils.merge_leading_dims(log_q_z, num_dims=2)

#     # Compute log prob of latents under the prior
#     log_p_z = model.prior.log_prob(latents)

#     # Compute log prob of inputs under the decoder,
#     inputs = torchutils.repeat_rows(inputs, num_reps=num_samples)
#     log_p_x = model.likelihood.log_prob(inputs, context=latents)

#     # Compute ELBO
#     elbo = log_p_x + kl_multiplier * (log_p_z - log_q_z)
#     elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])

#     if keepdim:
#         return elbo
#     else:
#         return torch.sum(elbo, dim=1) / num_samples  # Average ELBO across samples


# @set_default_tensor_type(torch.cuda.FloatTensor)
# def langevin_elbo(
#     model,
#     inputs: torch.Tensor,
#     cached_latents: torch.Tensor,
#     num_samples=1,
#     kl_multiplier=1,
#     keepdim=False,
# ):
#     # Sample latents and calculate their log prob under the encoder

#     # Get posterior mean and std parameters
#     if model.inputs_encoder is None:
#         posterior_context = inputs
#     else:
#         posterior_context = model.inputs_encoder(inputs)

#     latents = model.approximate_posterior._sample(
#         num_samples, posterior_context, cached_latents
#     )
#     # latents = torchutils.merge_leading_dims(latents, num_dims=2)

#     log_q_z = model.approximate_posterior.log_prob(latents, context=posterior_context)
#     # means, log_stds = model.approximate_posterior._compute_params(posterior_context)
#     # log_q_z = Normal(means, log_stds.exp()).log_prob(latents).sum(-1)
#     with torch.no_grad():
#         print(log_q_z.mean())

#     # Compute log prob of latents under the prior
#     log_p_z = model.prior.log_prob(latents)

#     # Compute log prob of inputs under the decoder,
#     inputs = torchutils.repeat_rows(inputs, num_reps=num_samples)
#     log_p_x = model.likelihood.log_prob(inputs, context=latents)

#     # Examine all components
#     print(f"log q(z|x): {log_q_z.mean()}")
#     print(f"log p(z): {log_p_z.mean()}")
#     print(f"log p(x|z): {log_p_x.mean()}")

#     # Compute ELBO
#     elbo = log_p_x + kl_multiplier * (log_p_z - log_q_z)

#     # Filter out bad samples
#     # elbo = elbo[log_q_z < -10_000]

#     elbo = torchutils.split_leading_dim(elbo, [-1, num_samples])

#     if keepdim:
#         return elbo, latents
#     else:
#         return (
#             torch.sum(elbo, dim=1) / num_samples,
#             latents,
#         )  # Average ELBO across samples
