import torch
import torch.nn as nn


class ResidualBlock1dConv(nn.Module):
    def __init__(
        self,
        channels_in,
        channels_out,
        kernelsize,
        stride,
        padding,
        dilation,
        downsample,
        a=2,
        b=0.3,
    ):
        super(ResidualBlock1dConv, self).__init__()
        self.bn1 = nn.BatchNorm1d(channels_in)
        self.conv1 = nn.Conv1d(
            channels_in, channels_in, kernel_size=1, stride=1, padding=0
        )
        self.dropout1 = nn.Dropout(p=0.5, inplace=False)
        self.relu = nn.ReLU(inplace=True)
        self.bn2 = nn.BatchNorm1d(channels_in)
        self.conv2 = nn.Conv1d(
            channels_in,
            channels_out,
            kernel_size=kernelsize,
            stride=stride,
            padding=padding,
            dilation=dilation,
        )
        self.dropout2 = nn.Dropout(p=0.5, inplace=False)
        self.downsample = downsample
        self.a = a
        self.b = b

    def forward(self, x):
        residual = x
        out = self.bn1(x)
        out = self.relu(out)
        out = self.conv1(out)
        out = self.dropout1(out)
        out = self.bn2(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.dropout2(out)
        if self.downsample:
            residual = self.downsample(x)
        out = self.a * residual + self.b * out
        return out


def make_res_block_encoder_feature_extractor(
    in_channels,
    out_channels,
    kernelsize,
    stride,
    padding,
    dilation,
    a_val=2.0,
    b_val=0.3,
):
    downsample = None
    if (stride != 1) or (in_channels != out_channels) or dilation != 1:
        downsample = nn.Sequential(
            nn.Conv1d(
                in_channels,
                out_channels,
                kernel_size=kernelsize,
                stride=stride,
                padding=padding,
                dilation=dilation,
            ),
            nn.BatchNorm1d(out_channels),
        )
    layers = []
    layers.append(
        ResidualBlock1dConv(
            in_channels,
            out_channels,
            kernelsize,
            stride,
            padding,
            dilation,
            downsample,
            a=a_val,
            b=b_val,
        )
    )
    return nn.Sequential(*layers)


class FeatureExtractorText(nn.Module):
    def __init__(self, a, b, num_features=71, DIM_text=128, enc_padding_text=1):
        super(FeatureExtractorText, self).__init__()
        self.a = a
        self.b = b
        self.conv1 = nn.Conv1d(
            num_features,
            DIM_text,
            kernel_size=4,
            stride=2,
            padding=enc_padding_text,
            dilation=1,
        )
        self.resblock_1 = make_res_block_encoder_feature_extractor(
            DIM_text,
            2 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_2 = make_res_block_encoder_feature_extractor(
            2 * DIM_text,
            3 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_3 = make_res_block_encoder_feature_extractor(
            3 * DIM_text,
            4 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_4 = make_res_block_encoder_feature_extractor(
            4 * DIM_text,
            5 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_5 = make_res_block_encoder_feature_extractor(
            5 * DIM_text,
            5 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_6 = make_res_block_encoder_feature_extractor(
            5 * DIM_text,
            5 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=0,
            dilation=1,
        )

    def forward(self, x):
        x = x.transpose(-2, -1)
        out = self.conv1(x)
        out = self.resblock_1(out)
        out = self.resblock_2(out)
        out = self.resblock_3(out)
        out = self.resblock_4(out)
        out = self.resblock_5(out)
        out = self.resblock_6(out)
        return out


class LinearFeatureCompressor(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(LinearFeatureCompressor, self).__init__()
        self.mu = nn.Linear(in_channels, out_channels, bias=True)
        self.logvar = nn.Linear(in_channels, out_channels, bias=True)

    def forward(self, feats):
        feats = feats.view(feats.size(0), -1)
        mu, logvar = self.mu(feats), self.logvar(feats)
        return mu, logvar


class ResidualBlock1dTransposeConv(nn.Module):
    def __init__(
        self,
        channels_in,
        channels_out,
        kernelsize,
        stride,
        padding,
        dilation,
        o_padding,
        upsample,
        a=2,
        b=0.3,
    ):
        super(ResidualBlock1dTransposeConv, self).__init__()
        self.bn1 = nn.BatchNorm1d(channels_in)
        self.conv1 = nn.ConvTranspose1d(
            channels_in, channels_in, kernel_size=1, stride=1, padding=0
        )
        self.dropout1 = nn.Dropout(p=0.5, inplace=False)
        self.relu = nn.ReLU(inplace=True)
        self.bn2 = nn.BatchNorm1d(channels_in)
        self.conv2 = nn.ConvTranspose1d(
            channels_in,
            channels_out,
            kernel_size=kernelsize,
            stride=stride,
            padding=padding,
            dilation=dilation,
            output_padding=o_padding,
        )
        self.dropout2 = nn.Dropout(p=0.5, inplace=False)
        self.upsample = upsample
        self.a = a
        self.b = b

    def forward(self, x):
        residual = x
        out = self.bn1(x)
        out = self.relu(out)
        out = self.conv1(out)
        out = self.dropout1(out)
        out = self.bn2(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.dropout2(out)
        if self.upsample:
            residual = self.upsample(x)
        out = self.a * residual + self.b * out
        return out


def make_res_block_decoder(
    in_channels,
    out_channels,
    kernelsize,
    stride,
    padding,
    o_padding,
    dilation,
    a_val=2.0,
    b_val=0.3,
):
    upsample = None

    if (
        (kernelsize != 1 or stride != 1)
        or (in_channels != out_channels)
        or dilation != 1
    ):
        upsample = nn.Sequential(
            nn.ConvTranspose1d(
                in_channels,
                out_channels,
                kernel_size=kernelsize,
                stride=stride,
                padding=padding,
                dilation=dilation,
                output_padding=o_padding,
            ),
            nn.BatchNorm1d(out_channels),
        )
    layers = []
    layers.append(
        ResidualBlock1dTransposeConv(
            in_channels,
            out_channels,
            kernelsize,
            stride,
            padding,
            dilation,
            o_padding,
            upsample=upsample,
            a=a_val,
            b=b_val,
        )
    )
    return nn.Sequential(*layers)


class DataGeneratorText(nn.Module):
    def __init__(self, a, b, DIM_text=128, num_features=71):
        super(DataGeneratorText, self).__init__()
        self.a = a
        self.b = b
        self.resblock_1 = make_res_block_decoder(
            5 * DIM_text,
            5 * DIM_text,
            kernelsize=4,
            stride=1,
            padding=0,
            dilation=1,
            o_padding=0,
        )
        self.resblock_2 = make_res_block_decoder(
            5 * DIM_text,
            5 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
            o_padding=0,
        )
        self.resblock_3 = make_res_block_decoder(
            5 * DIM_text,
            4 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
            o_padding=0,
        )
        self.resblock_4 = make_res_block_decoder(
            4 * DIM_text,
            3 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
            o_padding=0,
        )
        self.resblock_5 = make_res_block_decoder(
            3 * DIM_text,
            2 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
            o_padding=0,
        )
        self.resblock_6 = make_res_block_decoder(
            2 * DIM_text,
            DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
            o_padding=0,
        )
        self.conv2 = nn.ConvTranspose1d(
            DIM_text,
            num_features,
            kernel_size=4,
            stride=2,
            padding=1,
            dilation=1,
            output_padding=0,
        )
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, feats):
        d = self.resblock_1(feats)
        d = self.resblock_2(d)
        d = self.resblock_3(d)
        d = self.resblock_4(d)
        d = self.resblock_5(d)
        d = self.resblock_6(d)
        d = self.conv2(d)
        d = self.softmax(d)
        return d


class CelebaTextEncoder(nn.Module):
    def __init__(self, latent_dim=32, a_text=2.0, b_text=0.3, DIM_text=128):
        super().__init__()
        self.feature_extractor = FeatureExtractorText(a=a_text, b=b_text)
        self.feature_compressor = LinearFeatureCompressor(5 * DIM_text, latent_dim)

    def forward(self, x_text):
        h_text = self.feature_extractor(x_text)
        mu, logvar = self.feature_compressor(h_text)

        return torch.cat([mu, logvar], dim=-1)


class CelebaTextDecoder(nn.Module):
    def __init__(self, latent_dim=32, DIM_text=128, a_text=2.0, b_text=0.3):
        super().__init__()
        self.feature_generator = nn.Linear(latent_dim, 5 * DIM_text, bias=True)
        self.text_generator = DataGeneratorText(a=a_text, b=b_text)

    def forward(self, z):
        text_feat_hat = self.feature_generator(z)
        text_feat_hat = text_feat_hat.unsqueeze(-1)
        text_hat = self.text_generator(text_feat_hat)
        text_hat = text_hat.transpose(-2, -1)

        # text_hat: [B, len_sequence, len(alphabet)]
        # After LogSoftMax layer
        return text_hat


class CelebaTextClassifier(nn.Module):
    def __init__(
        self,
        DIM_text=128,
        num_features=71,
        enc_padding_text=1,
        num_layers_text=7,
    ):
        super().__init__()
        self.conv1 = nn.Conv1d(
            num_features,
            DIM_text,
            kernel_size=3,
            stride=2,
            padding=enc_padding_text,
            dilation=1,
        )
        self.resblock_1 = make_res_block_encoder_feature_extractor(
            DIM_text,
            2 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_2 = make_res_block_encoder_feature_extractor(
            2 * DIM_text,
            3 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_3 = make_res_block_encoder_feature_extractor(
            3 * DIM_text,
            4 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_4 = make_res_block_encoder_feature_extractor(
            4 * DIM_text,
            5 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_5 = make_res_block_encoder_feature_extractor(
            5 * DIM_text,
            6 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=1,
            dilation=1,
        )
        self.resblock_6 = make_res_block_encoder_feature_extractor(
            6 * DIM_text,
            7 * DIM_text,
            kernelsize=4,
            stride=2,
            padding=0,
            dilation=1,
        )
        self.dropout = nn.Dropout(p=0.5, inplace=False)
        self.linear = nn.Linear(
            in_features=num_layers_text * DIM_text,
            out_features=40,
            bias=True,
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self, x_text):
        x_text = x_text.transpose(-2, -1)
        out = self.conv1(x_text)
        out = self.resblock_1(out)
        out = self.resblock_2(out)
        out = self.resblock_3(out)
        out = self.resblock_4(out)
        out = self.resblock_5(out)
        out = self.resblock_6(out)
        h = self.dropout(out)
        h = h.view(h.size(0), -1)
        h = self.linear(h)
        out = self.sigmoid(h)

        # Multiclass classification / probabilities
        # [B, 40]
        return out
