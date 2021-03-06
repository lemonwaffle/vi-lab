"""
Taken from:
- https://github.com/juho-lee/set_transformer/blob/master/max_regression_demo.ipynb
"""
from typing import List, Optional, Tuple

import torch
import torch.nn as nn
from src.models.base import MLP
from src.models.dists import ConditionalDiagonalNormal
from .helpers import SAB, PMA

__all__ = [
    "ProductOfExpertsEncoder",
    "MultimodalEncoder",
    "PartitionedMultimodalEncoder",
    "SetEncoder",
    "DeepSet",
    "SetTransformer",
    "ConcatEncoder",
    "PoE_Encoder",
]


class ProductOfExpertsEncoder(nn.Module):
    def __init__(self, latent_dim: int, encoders: List[nn.Module]):
        """Composes multiple unimodal encoders into a multimodal encoder
        using Product of Experts (Gaussians).

        Parameters
        ----------
        latent_dim : int
        encoders : List[nn.Module]
            An encoder for each modality
        """
        super().__init__()

        # Init unimodal gaussian dists
        self.dists = nn.ModuleList(
            [
                ConditionalDiagonalNormal((latent_dim,), context_encoder=e)
                for e in encoders
            ]
        )

    def forward(self, xs: List[Optional[torch.Tensor]]):
        """
        Parameters
        ----------
        xs : List[Optional[torch.Tensor]]
            An input for each encoder. Allows for missing modalities.
            E.g. [x, y] or [x, None] or [None, y]
        """
        means = []
        log_stds = []

        assert len(self.dists) == len(
            xs
        ), "Number of encoders and inputs must be the same!"

        # Compute params for each unimodal dist
        for dist, x in zip(self.dists, xs):
            # Ignore for missing modalities
            if x is None:
                continue

            m, s = dist._compute_params(x)
            means.append(m)
            log_stds.append(s)

        # Add params of prior expert; assume prior is standard normal
        # FIXME What if specify different prior?
        means.append(torch.zeros_like(means[0]))
        log_stds.append(torch.zeros_like(log_stds[0]))

        # Combine params using Product of Experts
        pd_means, pd_log_stds = self._product_of_experts(
            torch.stack(means), torch.stack(log_stds)
        )

        return torch.cat([pd_means, pd_log_stds], dim=-1)

    def _product_of_experts(
        self, means: torch.Tensor, log_stds: torch.Tensor, eps=1e-8
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Return parameters for product of K independent experts.
        See https://arxiv.org/pdf/1410.7827.pdf for equations.

        Parameters
        ----------
        means : torch.Tensor
            [K, B, D]
        log_stds : torch.Tensor
            [K, B, D]
        eps : float, optional
            , by default 1e-8

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            means,  log_stds
            [B, D], [B, D]
        """
        # FIXME Double-check
        log_vars = log_stds * 2
        var = torch.exp(log_vars) + eps

        # precision of i-th Gaussian expert at point x
        T = 1.0 / (var + eps)
        pd_means = torch.sum(means * T, dim=0) / torch.sum(T, dim=0)
        pd_vars = 1.0 / torch.sum(T, dim=0)
        pd_log_stds = torch.log(pd_vars + eps) / 2

        return pd_means, pd_log_stds


class MultimodalEncoder(nn.Module):
    def __init__(self, encoders: List[nn.Module], fusion_module: nn.Module):
        """Composes multiple unimodal encoders into a multimodal encoder
        using a fusion module

        Assumes all unimodal encoders encode each modality into a feature
        vector of the same dim

        Parameters
        ----------
        encoders : List[nn.Module]
            An encoder for each modality
        fusion_module : nn.Module
            An encoder that fuses multiple inputs into a single feature vector.
        """
        super().__init__()

        self.encoders = nn.ModuleList(encoders)
        self.fusion_module = fusion_module

    def forward(self, xs: List[Optional[torch.Tensor]]):
        """
        Parameters
        ----------
        xs : List[Optional[torch.Tensor]]
            An input for each encoder. Allows for missing modalities.
            E.g. [x, y] or [x, None] or [None, y]
        """
        outputs = []

        # Get output from each encoder
        for x, encoder in zip(xs, self.encoders):
            # Ignore for missing modalities
            if x is None:
                outputs.append(None)

            else:
                outputs.append(encoder(x))

        # Perform multimodal fusion
        return self.fusion_module(outputs)


class PartitionedMultimodalEncoder(nn.Module):
    def __init__(self, encoders: List[nn.Module], fusion_module: nn.Module):
        """Composes multiple unimodal (partitioned) encoders into a multimodal
        encoder using a fusion module.

        Outputs both modality-specific and -shared latent vectors.

        Assumes all unimodal encoders encode each modality into a shared feature
        vector of the same dim

        Parameters
        ----------
        encoders : List[nn.Module]
            An encoder for each modality
        fusion_module : nn.Module
            An encoder that fuses multiple inputs into a single feature vector.
        """
        super().__init__()

        self.encoders = nn.ModuleList(encoders)
        self.fusion_module = fusion_module

    def forward(self, xs: List[Optional[torch.Tensor]]):
        """
        Parameters
        ----------
        xs : List[Optional[torch.Tensor]]
            An input for each encoder. Allows for missing modalities.
            E.g. [x, y] or [x, None] or [None, y]
        """
        m_latents = []
        s_latents = []

        # Get output from each encoder
        for x, encoder in zip(xs, self.encoders):
            # Ignore modality-specific latents for missing modalities
            if x is None:
                m_latents.append(None)
                s_latents.append(None)
            else:
                latents = encoder(x)
                m_latents.append(latents["m"])
                s_latents.append(latents["s"])

        # Perform multimodal fusion for shared latents
        return {"m": m_latents, "s": self.fusion_module(s_latents)}


class SetEncoder(MLP):
    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_units: List[int],
        activation: str = "ReLU",
        operator: str = "max",
    ):
        """Encoder that is permutation, and cardinality invariant.

        Set of inputs -> Pooling operator -> MLP

        Parameters
        ----------
        input_size : int
            Input dim of each separate input (assumed to be of same size)
        output_size : int
        hidden_units : List[int]
            To specify hidden units of the MLP
        activation : str, optional
            Activation function between MLP layers, by default "ReLU"
        operator: str, optional
            Set pooling operator, by default "max"
            One of {"max", "sum", "mean", "min", "median"}
        """
        # Set pooling operator
        def in_lambda(xs: List[Optional[torch.Tensor]]):
            # Filter out missing inputs
            x = torch.stack([x for x in xs if x is not None], dim=0)

            if operator == "max":
                x, _ = x.max(dim=0)
                return x

            elif operator == "mean":
                pass

            elif operator == "sum":
                return x.sum(dim=0)

        super().__init__(
            input_size,
            output_size,
            hidden_units=hidden_units,
            activation=activation,
            in_lambda=in_lambda,
        )


class DeepSet(nn.Module):
    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_size: int,
        n_enc_layers: int = 3,
        n_dec_layers: int = 1,
        pool="max",
    ):
        super().__init__()
        # FIXME To be able to specify num_outputs
        self.enc = MLP(
            input_size,
            hidden_size,
            [hidden_size] * n_enc_layers,
        )
        self.dec = MLP(
            hidden_size,
            output_size,
            [hidden_size] * n_dec_layers,
        )
        self.pool = pool

    def forward(self, xs) -> None:
        """Takes as input a set of vectors

        Parameters
        ----------
        x : List[Optional[torch.Tensor]]
            List[B, input_size]

        Returns
        -------
        torch.Tensor
            [B, output_size]
        """
        # [B, set_size, input_size]
        x = torch.stack([x for x in xs if x is not None], dim=1)

        x = self.enc(x)

        if self.pool == "max":
            x = x.max(dim=1)[0]
        elif self.pool == "mean":
            x = x.mean(dim=1)
        elif self.pool == "sum":
            x = x.sum(dim=1)

        x = self.dec(x)

        return x


class SetTransformer(nn.Module):
    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_size: int,
        n_hidden_layers: int = 1,
        n_heads=4,
        ln=False,
        modality_embeddings=False,
    ):
        super().__init__()

        # Modality-specific embeddings
        self.modality_embeddings = modality_embeddings
        n_modalities = 2
        self.embs = (
            nn.Embedding(n_modalities, input_size) if modality_embeddings else None
        )

        # FIXME Don't do layer_norm for first layer?
        # Take reference from original implementation
        enc_list = [SAB(input_size, hidden_size, n_heads, ln)]
        for _ in range(n_hidden_layers):
            enc_list.append(SAB(hidden_size, hidden_size, n_heads, ln))
        self.enc = nn.Sequential(*enc_list)

        self.dec = nn.Sequential(
            PMA(hidden_size, n_heads, num_seeds=1, ln=ln),
            # FIXME More linear layers? Dropout?
            nn.Linear(hidden_size, output_size),
        )

    def forward(self, xs):
        """Takes as input a set of vectors

        Parameters
        ----------
        x : List[Optional[torch.Tensor]]
            List[B, input_size]

        Returns
        -------
        torch.Tensor
            [B, output_size]
        """
        x_list = []
        # FIXME Any way to vectorize this?
        for idx, x in enumerate(xs):
            # Filter out missing modalities
            if x is None:
                continue

            # Add modality-specific embedding
            if self.modality_embeddings:
                idx_tensor = torch.tensor([idx], device=x.device)
                x += self.embs(idx_tensor)

            x_list.append(x)

        # [B, set_size, input_size]
        x = torch.stack(x_list, dim=1)

        x = self.enc(x)
        x = self.dec(x)

        return x.squeeze(1)


class ConcatEncoder(MLP):
    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_units: List[int],
        n_inputs: int = 2,
        activation: str = "ReLU",
    ):
        """Encoder that performs concat fusion, and handles missing inputs
        by setting them to zero

        Concat inputs -> MLP

        Parameters
        ----------
        input_size : int
            Input dim of each separate input (assumed to be of same size)
        output_size : int
        hidden_units : List[int]
            To specify hidden units of the MLP
        n_inputs : int
            Specify number of inputs / modalities
        activation : str, optional
            Activation function between MLP layers, by default "ReLU"
        """
        # Concat features; set missing inputs to zero
        def in_lambda(xs: List[Optional[torch.Tensor]]):
            # Get sample vector
            sample = None
            for x in xs:
                if x is not None:
                    sample = x
                    break

            assert sample is not None, "There should be at least one available input"

            # Replace missing inputs with zeros
            xs_padded = [(x if x is not None else torch.zeros_like(sample)) for x in xs]

            # Concat along feature dimension
            return torch.cat(xs_padded, dim=-1)

        super().__init__(
            # Input layer is D * n_inputs
            input_size * n_inputs,
            output_size,
            hidden_units=hidden_units,
            activation=activation,
            in_lambda=in_lambda,
        )


class PoE_Encoder(nn.Module):
    def forward(self, xs: List[Optional[torch.Tensor]]):
        """
        Parameters
        ----------
        xs : List[Optional[torch.Tensor]]
            An input for each encoder. Allows for missing modalities.
            E.g. [x, y] or [x, None] or [None, y]
        """
        means = []
        log_stds = []

        # Extract out means and log_stds
        for x in xs:
            if x is None:
                continue

            m, s = x.chunk(2, dim=-1)
            means.append(m)
            log_stds.append(s)

        # Add params of prior expert; assume prior is standard normal
        # FIXME What if specify different prior?
        means.append(torch.zeros_like(means[0]))
        log_stds.append(torch.zeros_like(log_stds[0]))

        # Combine params using Product of Experts
        pd_means, pd_log_stds = self._product_of_experts(
            torch.stack(means), torch.stack(log_stds)
        )

        return torch.cat([pd_means, pd_log_stds], dim=-1)

    def _product_of_experts(
        self, means: torch.Tensor, log_stds: torch.Tensor, eps=1e-8
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Return parameters for product of K independent experts.
        See https://arxiv.org/pdf/1410.7827.pdf for equations.

        Parameters
        ----------
        means : torch.Tensor
            [K, B, D]
        log_stds : torch.Tensor
            [K, B, D]
        eps : float, optional
            , by default 1e-8

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            means,  log_stds
            [B, D], [B, D]
        """
        # FIXME Double-check
        log_vars = log_stds * 2
        var = torch.exp(log_vars) + eps

        # precision of i-th Gaussian expert at point x
        T = 1.0 / (var + eps)
        pd_means = torch.sum(means * T, dim=0) / torch.sum(T, dim=0)
        pd_vars = 1.0 / torch.sum(T, dim=0)
        pd_log_stds = torch.log(pd_vars + eps) / 2

        return pd_means, pd_log_stds
