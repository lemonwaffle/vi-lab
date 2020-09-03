import torch.nn as nn
import torch.nn.functional as F
from torch.nn.functional import dropout


class ResidualBlock(nn.Module):
    def __init__(
        self,
        in_channels,
        resample=None,
        activation=F.relu,
        dropout_probability=0.0,
        first=False,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.resample = resample
        self.activation = activation

        self.residual_layer_1 = nn.Conv2d(
            in_channels=in_channels, out_channels=in_channels, kernel_size=3, padding=1
        )

        if resample is None:
            self.shortcut_layer = nn.Identity()
            self.residual_2_layer = nn.Conv2d(
                in_channels=in_channels,
                out_channels=in_channels,
                kernel_size=3,
                padding=1,
            )
        elif resample == "down":
            self.shortcut_layer = nn.Conv2d(
                in_channels=in_channels,
                out_channels=2 * in_channels,
                kernel_size=3,
                stride=2,
                padding=1,
            )
            self.residual_2_layer = nn.Conv2d(
                in_channels=in_channels,
                out_channels=2 * in_channels,
                kernel_size=3,
                stride=2,
                padding=1,
            )
        elif resample == "up":
            self.shortcut_layer = nn.ConvTranspose2d(
                in_channels=in_channels,
                out_channels=in_channels // 2,
                kernel_size=3,
                stride=2,
                padding=1,
                output_padding=0 if first else 1,
            )
            self.residual_2_layer = nn.ConvTranspose2d(
                in_channels=in_channels,
                out_channels=in_channels // 2,
                kernel_size=3,
                stride=2,
                padding=1,
                output_padding=0 if first else 1,
            )

        if dropout_probability > 0:
            self.dropout = nn.Dropout(dropout_probability)
        else:
            self.dropout = None

    def forward(self, inputs):

        shortcut = self.shortcut_layer(inputs)
        residual_1 = self.activation(inputs)
        residual_1 = self.residual_layer_1(residual_1)
        if self.dropout is not None:
            residual_1 = self.dropout(residual_1)
        residual_2 = self.activation(residual_1)
        residual_2 = self.residual_2_layer(residual_2)

        return shortcut + residual_2


class ConvEncoder(nn.Module):
    def __init__(
        self,
        context_features,
        channels_multiplier,
        activation=F.relu,
        dropout_probability=0.0,
    ):
        super().__init__()
        self.context_features = context_features
        self.channels_multiplier = channels_multiplier
        self.activation = activation

        self.initial_layer = nn.Conv2d(1, channels_multiplier, kernel_size=1)
        self.residual_blocks = nn.ModuleList(
            [
                ResidualBlock(
                    in_channels=channels_multiplier,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier,
                    resample="down",
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 2,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 2,
                    resample="down",
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 4,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 4,
                    resample="down",
                    dropout_probability=dropout_probability,
                ),
            ]
        )
        self.final_layer = nn.Linear(
            in_features=(4 * 4 * channels_multiplier * 8), out_features=context_features
        )

    def forward(self, inputs):
        temps = self.initial_layer(inputs)
        for residual_block in self.residual_blocks:
            temps = residual_block(temps)
        temps = self.activation(temps)
        outputs = self.final_layer(
            temps.reshape(-1, 4 * 4 * self.channels_multiplier * 8)
        )
        return outputs


class ConvDecoder(nn.Module):
    def __init__(
        self,
        latent_features,
        channels_multiplier,
        activation=F.relu,
        dropout_probability=0.0,
    ):
        super().__init__()
        self.latent_features = latent_features
        self.channels_multiplier = channels_multiplier
        self.activation = activation

        self.initial_layer = nn.Linear(
            in_features=latent_features, out_features=(4 * 4 * channels_multiplier * 8)
        )
        self.residual_blocks = nn.ModuleList(
            [
                ResidualBlock(
                    in_channels=channels_multiplier * 8,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 8,
                    resample="up",
                    first=True,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 4,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 4,
                    resample="up",
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 2,
                    dropout_probability=dropout_probability,
                ),
                ResidualBlock(
                    in_channels=channels_multiplier * 2,
                    resample="up",
                    dropout_probability=dropout_probability,
                ),
            ]
        )
        self.final_layer = nn.Conv2d(
            in_channels=channels_multiplier, out_channels=1, kernel_size=1
        )

    def forward(self, inputs):
        temps = self.initial_layer(inputs).reshape(
            -1, self.channels_multiplier * 8, 4, 4
        )
        for residual_block in self.residual_blocks:
            temps = residual_block(temps)
        temps = self.activation(temps)
        outputs = self.final_layer(temps)
        return outputs


def conv_encoder(n_outputs: int, dropout_prob=0.0):
    return ConvEncoder(
        context_features=n_outputs,
        channels_multiplier=16,
        dropout_probability=dropout_prob,
    )

