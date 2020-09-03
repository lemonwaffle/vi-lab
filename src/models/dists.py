import torch.nn as nn
import torch.nn.functional as F
from nflows import transforms
from nflows.distributions import (
    ConditionalDiagonalNormal,
    ConditionalIndependentBernoulli,
    Distribution,
    StandardNormal,
)
from nflows.flows import Flow
from nflows.nn.nets import ResidualNet
from nflows.transforms import Transform
from nflows.utils import torchutils

from .convs import ConvDecoder, ConvEncoder


# HELPER TRANSFORMS ############################################################
def create_lu_linear(n_dim: int) -> Transform:
    return transforms.CompositeTransform(
        [
            transforms.RandomPermutation(n_dim),
            transforms.LULinear(n_dim, identity_init=True),
        ]
    )


def create_rq_coupling(
    n_dim: int,
    step_index: int,
    context_features=None,
    n_bins=8,
    tail_bound=3,
    apply_unconditional_transform=True,
    # Hypernet args
    n_hidden=128,
    n_transform_blocks=2,
    dropout_prob=0.0,
    use_batch_norm=False,
) -> Transform:
    return transforms.PiecewiseRationalQuadraticCouplingTransform(
        mask=torchutils.create_alternating_binary_mask(
            features=n_dim, even=(step_index % 2 == 0)
        ),
        transform_net_create_fn=lambda in_features, out_features: ResidualNet(
            in_features=in_features,
            out_features=out_features,
            hidden_features=n_hidden,
            context_features=context_features,
            num_blocks=n_transform_blocks,
            activation=F.relu,
            dropout_probability=dropout_prob,
            use_batch_norm=use_batch_norm,
        ),
        num_bins=n_bins,
        tails="linear",
        tail_bound=tail_bound,
        apply_unconditional_transform=apply_unconditional_transform,
    )


# PRIORS #######################################################################
def standard_normal(n_dim: int) -> Distribution:
    return StandardNormal((n_dim,))


def standard_flow(n_dim: int, n_flow_steps=10) -> Distribution:
    """StandardNormal -> LU -> RQ (C) -> ... -> LU"""
    base_dist = StandardNormal((n_dim,))
    transform = transforms.CompositeTransform(
        [
            transforms.CompositeTransform(
                [create_lu_linear(n_dim), create_rq_coupling(n_dim, i)]
            )
            for i in range(n_flow_steps)
        ]
    )
    transform = transforms.CompositeTransform([transform, create_lu_linear(n_dim)])

    return Flow(transform, base_dist)


# APPROX POSTERIORS ############################################################
def cond_diagonal_normal(n_dim: int, dropout_prob=0.0) -> Distribution:
    context_encoder = ConvEncoder(
        context_features=n_dim * 2,
        channels_multiplier=16,
        dropout_probability=dropout_prob,
    )
    return ConditionalDiagonalNormal(shape=[n_dim], context_encoder=context_encoder)


def cond_flow(n_dim: int, n_flow_steps=10, dropout_prob=0.0) -> Distribution:
    context_features = n_dim * 2

    # FIXME What is the context encoder here?
    context_encoder = nn.Linear(context_features, n_dim * 2)

    base_dist = ConditionalDiagonalNormal(
        shape=[n_dim], context_encoder=context_encoder
    )

    transform = transforms.CompositeTransform(
        [
            transforms.CompositeTransform(
                [
                    create_lu_linear(n_dim),
                    create_rq_coupling(n_dim, i, context_features=context_features),
                ]
            )
            for i in range(n_flow_steps)
        ]
    )
    transform = transforms.CompositeTransform([transform, create_lu_linear(n_dim)])

    # FIXME Why inverse?
    return Flow(transforms.InverseTransform(transform), base_dist)


# LIKELIHOODS ##################################################################
def cond_indpt_bernoulli(n_dim: int, dropout_prob=0.0):
    latent_decoder = ConvDecoder(
        latent_features=n_dim, channels_multiplier=16, dropout_probability=dropout_prob,
    )

    return ConditionalIndependentBernoulli(
        shape=[1, 28, 28], context_encoder=latent_decoder
    )
