# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math

import librosa
import paddle
from paddle import nn
from paddle.fluid.layers import sequence_mask
from paddle.nn import functional as F
from scipy import signal

from paddlespeech.t2s.modules.nets_utils import make_non_pad_mask


# Loss for new Tacotron2
class GuidedAttentionLoss(nn.Layer):
    """Guided attention loss function module.

    This module calculates the guided attention loss described
    in `Efficiently Trainable Text-to-Speech System Based
    on Deep Convolutional Networks with Guided Attention`_,
    which forces the attention to be diagonal.

    .. _`Efficiently Trainable Text-to-Speech System
        Based on Deep Convolutional Networks with Guided Attention`:
        https://arxiv.org/abs/1710.08969

    """

    def __init__(self, sigma=0.4, alpha=1.0, reset_always=True):
        """Initialize guided attention loss module.

        Parameters
        ----------
        sigma : float, optional
            Standard deviation to control how close attention to a diagonal.
        alpha : float, optional
            Scaling coefficient (lambda).
        reset_always : bool, optional
            Whether to always reset masks.

        """
        super().__init__()
        self.sigma = sigma
        self.alpha = alpha
        self.reset_always = reset_always
        self.guided_attn_masks = None
        self.masks = None

    def _reset_masks(self):
        self.guided_attn_masks = None
        self.masks = None

    def forward(self, att_ws, ilens, olens):
        """Calculate forward propagation.

        Parameters
        ----------
        att_ws : Tensor
            Batch of attention weights (B, T_max_out, T_max_in).
        ilens : Tensor(int64)
            Batch of input lenghts (B,).
        olens : Tensor(int64)
            Batch of output lenghts (B,).

        Returns
        ----------
        Tensor
            Guided attention loss value.

        """
        if self.guided_attn_masks is None:
            self.guided_attn_masks = self._make_guided_attention_masks(ilens,
                                                                       olens)
        if self.masks is None:
            self.masks = self._make_masks(ilens, olens)
        losses = self.guided_attn_masks * att_ws
        loss = paddle.mean(
            losses.masked_select(self.masks.broadcast_to(losses.shape)))
        if self.reset_always:
            self._reset_masks()
        return self.alpha * loss

    def _make_guided_attention_masks(self, ilens, olens):
        n_batches = len(ilens)
        max_ilen = max(ilens)
        max_olen = max(olens)
        guided_attn_masks = paddle.zeros((n_batches, max_olen, max_ilen))

        for idx, (ilen, olen) in enumerate(zip(ilens, olens)):
            guided_attn_masks[idx, :olen, :
                              ilen] = self._make_guided_attention_mask(
                                  ilen, olen, self.sigma)
        return guided_attn_masks

    @staticmethod
    def _make_guided_attention_mask(ilen, olen, sigma):
        """Make guided attention mask.

        Examples
        ----------
        >>> guided_attn_mask =_make_guided_attention(5, 5, 0.4)
        >>> guided_attn_mask.shape
        [5, 5]
        >>> guided_attn_mask
        tensor([[0.0000, 0.1175, 0.3935, 0.6753, 0.8647],
                [0.1175, 0.0000, 0.1175, 0.3935, 0.6753],
                [0.3935, 0.1175, 0.0000, 0.1175, 0.3935],
                [0.6753, 0.3935, 0.1175, 0.0000, 0.1175],
                [0.8647, 0.6753, 0.3935, 0.1175, 0.0000]])
        >>> guided_attn_mask =_make_guided_attention(3, 6, 0.4)
        >>> guided_attn_mask.shape
        [6, 3]
        >>> guided_attn_mask
        tensor([[0.0000, 0.2934, 0.7506],
                [0.0831, 0.0831, 0.5422],
                [0.2934, 0.0000, 0.2934],
                [0.5422, 0.0831, 0.0831],
                [0.7506, 0.2934, 0.0000],
                [0.8858, 0.5422, 0.0831]])

        """
        grid_x, grid_y = paddle.meshgrid(
            paddle.arange(olen), paddle.arange(ilen))
        grid_x = grid_x.cast(dtype=paddle.float32)
        grid_y = grid_y.cast(dtype=paddle.float32)
        return 1.0 - paddle.exp(-(
            (grid_y / ilen - grid_x / olen)**2) / (2 * (sigma**2)))

    @staticmethod
    def _make_masks(ilens, olens):
        """Make masks indicating non-padded part.

        Parameters
        ----------
        ilens : Tensor(int64) or List
            Batch of lengths (B,).
        olens : Tensor(int64) or List
            Batch of lengths (B,).

        Returns
        ----------
        Tensor
            Mask tensor indicating non-padded part.

        Examples
        ----------
        >>> ilens, olens = [5, 2], [8, 5]
        >>> _make_mask(ilens, olens)
        tensor([[[1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1]],

                [[1, 1, 0, 0, 0],
                [1, 1, 0, 0, 0],
                [1, 1, 0, 0, 0],
                [1, 1, 0, 0, 0],
                [1, 1, 0, 0, 0],
                [0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0]]], dtype=paddle.uint8)

        """
        # (B, T_in)
        in_masks = make_non_pad_mask(ilens)
        # (B, T_out)
        out_masks = make_non_pad_mask(olens)
        # (B, T_out, T_in)

        return paddle.logical_and(
            out_masks.unsqueeze(-1), in_masks.unsqueeze(-2))


class GuidedMultiHeadAttentionLoss(GuidedAttentionLoss):
    """Guided attention loss function module for multi head attention.

    Parameters
    ----------
    sigma : float, optional
        Standard deviation to controlGuidedAttentionLoss
        how close attention to a diagonal.
    alpha : float, optional
        Scaling coefficient (lambda).
    reset_always : bool, optional
        Whether to always reset masks.

    """

    def forward(self, att_ws, ilens, olens):
        """Calculate forward propagation.

        Parameters
        ----------
        att_ws : Tensor
            Batch of multi head attention weights (B, H, T_max_out, T_max_in).
        ilens : Tensor
            Batch of input lenghts (B,).
        olens : Tensor
            Batch of output lenghts (B,).

        Returns
        ----------
        Tensor
            Guided attention loss value.

        """
        if self.guided_attn_masks is None:
            self.guided_attn_masks = (
                self._make_guided_attention_masks(ilens, olens).unsqueeze(1))
        if self.masks is None:
            self.masks = self._make_masks(ilens, olens).unsqueeze(1)
        losses = self.guided_attn_masks * att_ws
        loss = paddle.mean(
            losses.masked_select(self.masks.broadcast_to(losses.shape)))
        if self.reset_always:
            self._reset_masks()

        return self.alpha * loss


class Tacotron2Loss(nn.Layer):
    """Loss function module for Tacotron2."""

    def __init__(self,
                 use_masking=True,
                 use_weighted_masking=False,
                 bce_pos_weight=20.0):
        """Initialize Tactoron2 loss module.
        Parameters
        ----------
        use_masking : bool
            Whether to apply masking for padded part in loss calculation.
        use_weighted_masking : bool
            Whether to apply weighted masking in loss calculation.
        bce_pos_weight : float
            Weight of positive sample of stop token.
        """
        super().__init__()
        assert (use_masking != use_weighted_masking) or not use_masking
        self.use_masking = use_masking
        self.use_weighted_masking = use_weighted_masking

        # define criterions
        reduction = "none" if self.use_weighted_masking else "mean"
        self.l1_criterion = nn.L1Loss(reduction=reduction)
        self.mse_criterion = nn.MSELoss(reduction=reduction)
        self.bce_criterion = nn.BCEWithLogitsLoss(
            reduction=reduction, pos_weight=paddle.to_tensor(bce_pos_weight))

    def forward(self, after_outs, before_outs, logits, ys, stop_labels, olens):
        """Calculate forward propagation.
        Parameters
        ----------
        after_outs : Tensor
            Batch of outputs after postnets (B, Lmax, odim).
        before_outs : Tensor
            Batch of outputs before postnets (B, Lmax, odim).
        logits : Tensor
            Batch of stop logits (B, Lmax).
        ys : Tensor
            Batch of padded target features (B, Lmax, odim).
        stop_labels : Tensor(int64)
            Batch of the sequences of stop token labels (B, Lmax).
        olens : Tensor(int64)
            Batch of the lengths of each target (B,).
        Returns
        ----------
        Tensor
            L1 loss value.
        Tensor
            Mean square error loss value.
        Tensor
            Binary cross entropy loss value.
        """
        # make mask and apply it
        if self.use_masking:
            masks = make_non_pad_mask(olens).unsqueeze(-1)
            ys = ys.masked_select(masks.broadcast_to(ys.shape))
            after_outs = after_outs.masked_select(
                masks.broadcast_to(after_outs.shape))
            before_outs = before_outs.masked_select(
                masks.broadcast_to(before_outs.shape))
            stop_labels = stop_labels.masked_select(
                masks[:, :, 0].broadcast_to(stop_labels.shape))
            logits = logits.masked_select(
                masks[:, :, 0].broadcast_to(logits.shape))

        # calculate loss
        l1_loss = self.l1_criterion(after_outs, ys) + self.l1_criterion(
            before_outs, ys)
        mse_loss = self.mse_criterion(after_outs, ys) + self.mse_criterion(
            before_outs, ys)
        bce_loss = self.bce_criterion(logits, stop_labels)

        # make weighted mask and apply it
        if self.use_weighted_masking:
            masks = make_non_pad_mask(olens).unsqueeze(-1)
            weights = masks.float() / masks.sum(axis=1, keepdim=True).float()
            out_weights = weights.divide(
                paddle.shape(ys)[0] * paddle.shape(ys)[2])
            logit_weights = weights.divide(paddle.shape(ys)[0])

            # apply weight
            l1_loss = l1_loss.multiply(out_weights)
            l1_loss = l1_loss.masked_select(masks.broadcast_to(l1_loss)).sum()
            mse_loss = mse_loss.multiply(out_weights)
            mse_loss = mse_loss.masked_select(
                masks.broadcast_to(mse_loss)).sum()
            bce_loss = bce_loss.multiply(logit_weights.squeeze(-1))
            bce_loss = bce_loss.masked_select(
                masks.squeeze(-1).broadcast_to(bce_loss)).sum()

        return l1_loss, mse_loss, bce_loss


# Loss for Tacotron2
def attention_guide(dec_lens, enc_lens, N, T, g, dtype=None):
    """Build that W matrix. shape(B, T_dec, T_enc)
    W[i, n, t] = 1 - exp(-(n/dec_lens[i] - t/enc_lens[i])**2 / (2g**2)) 

    See also:
    Tachibana, Hideyuki, Katsuya Uenoyama, and Shunsuke Aihara. 2017. “Efficiently Trainable Text-to-Speech System Based on Deep Convolutional Networks with Guided Attention.” ArXiv:1710.08969 [Cs, Eess], October. http://arxiv.org/abs/1710.08969.
    """
    dtype = dtype or paddle.get_default_dtype()
    dec_pos = paddle.arange(0, N).astype(dtype) / dec_lens.unsqueeze(
        -1)  # n/N # shape(B, T_dec)
    enc_pos = paddle.arange(0, T).astype(dtype) / enc_lens.unsqueeze(
        -1)  # t/T # shape(B, T_enc)
    W = 1 - paddle.exp(-(dec_pos.unsqueeze(-1) - enc_pos.unsqueeze(1))**2 /
                       (2 * g**2))

    dec_mask = sequence_mask(dec_lens, maxlen=N)
    enc_mask = sequence_mask(enc_lens, maxlen=T)
    mask = dec_mask.unsqueeze(-1) * enc_mask.unsqueeze(1)
    mask = paddle.cast(mask, W.dtype)

    W *= mask
    return W


def guided_attention_loss(attention_weight, dec_lens, enc_lens, g):
    """Guided attention loss, masked to excluded padding parts."""
    _, N, T = attention_weight.shape
    W = attention_guide(dec_lens, enc_lens, N, T, g, attention_weight.dtype)

    total_tokens = (dec_lens * enc_lens).astype(W.dtype)
    loss = paddle.mean(paddle.sum(W * attention_weight, [1, 2]) / total_tokens)
    return loss


# Losses for GAN Vocoder
def stft(x,
         fft_size,
         hop_length=None,
         win_length=None,
         window='hann',
         center=True,
         pad_mode='reflect'):
    """Perform STFT and convert to magnitude spectrogram.
    Parameters
    ----------
    x : Tensor
        Input signal tensor (B, T).
    fft_size : int
        FFT size.
    hop_size : int
        Hop size.
    win_length : int
        window : str, optional
    window : str
        Name of window function, see `scipy.signal.get_window` for more
        details. Defaults to "hann".
    center : bool, optional
        center (bool, optional): Whether to pad `x` to make that the
        :math:`t \times hop\\_length` at the center of :math:`t`-th frame. Default: `True`.
    pad_mode : str, optional
        Choose padding pattern when `center` is `True`.
    Returns
    ----------
    Tensor:
        Magnitude spectrogram (B, #frames, fft_size // 2 + 1).
    """
    # calculate window
    window = signal.get_window(window, win_length, fftbins=True)
    window = paddle.to_tensor(window)
    x_stft = paddle.signal.stft(
        x,
        fft_size,
        hop_length,
        win_length,
        window=window,
        center=center,
        pad_mode=pad_mode)

    real = x_stft.real()
    imag = x_stft.imag()

    return paddle.sqrt(paddle.clip(real**2 + imag**2, min=1e-7)).transpose(
        [0, 2, 1])


class SpectralConvergenceLoss(nn.Layer):
    """Spectral convergence loss module."""

    def __init__(self):
        """Initilize spectral convergence loss module."""
        super().__init__()

    def forward(self, x_mag, y_mag):
        """Calculate forward propagation.
        Parameters
        ----------
        x_mag : Tensor
            Magnitude spectrogram of predicted signal (B, #frames, #freq_bins).
        y_mag : Tensor)
            Magnitude spectrogram of groundtruth signal (B, #frames, #freq_bins).
        Returns
        ----------
        Tensor
            Spectral convergence loss value.
        """
        return paddle.norm(
            y_mag - x_mag, p="fro") / paddle.clip(
                paddle.norm(y_mag, p="fro"), min=1e-10)


class LogSTFTMagnitudeLoss(nn.Layer):
    """Log STFT magnitude loss module."""

    def __init__(self, epsilon=1e-7):
        """Initilize los STFT magnitude loss module."""
        super().__init__()
        self.epsilon = epsilon

    def forward(self, x_mag, y_mag):
        """Calculate forward propagation.
        Parameters
        ----------
        x_mag : Tensor
            Magnitude spectrogram of predicted signal (B, #frames, #freq_bins).
        y_mag : Tensor
            Magnitude spectrogram of groundtruth signal (B, #frames, #freq_bins).
        Returns
        ----------
        Tensor
            Log STFT magnitude loss value.
        """
        return F.l1_loss(
            paddle.log(paddle.clip(y_mag, min=self.epsilon)),
            paddle.log(paddle.clip(x_mag, min=self.epsilon)))


class STFTLoss(nn.Layer):
    """STFT loss module."""

    def __init__(self,
                 fft_size=1024,
                 shift_size=120,
                 win_length=600,
                 window="hann"):
        """Initialize STFT loss module."""
        super().__init__()
        self.fft_size = fft_size
        self.shift_size = shift_size
        self.win_length = win_length
        self.window = window
        self.spectral_convergence_loss = SpectralConvergenceLoss()
        self.log_stft_magnitude_loss = LogSTFTMagnitudeLoss()

    def forward(self, x, y):
        """Calculate forward propagation.
        Parameters
        ----------
        x : Tensor
            Predicted signal (B, T).
        y : Tensor
            Groundtruth signal (B, T).
        Returns
        ----------
        Tensor
            Spectral convergence loss value.
        Tensor
            Log STFT magnitude loss value.
        """
        x_mag = stft(x, self.fft_size, self.shift_size, self.win_length,
                     self.window)
        y_mag = stft(y, self.fft_size, self.shift_size, self.win_length,
                     self.window)
        sc_loss = self.spectral_convergence_loss(x_mag, y_mag)
        mag_loss = self.log_stft_magnitude_loss(x_mag, y_mag)

        return sc_loss, mag_loss


class MultiResolutionSTFTLoss(nn.Layer):
    """Multi resolution STFT loss module."""

    def __init__(
            self,
            fft_sizes=[1024, 2048, 512],
            hop_sizes=[120, 240, 50],
            win_lengths=[600, 1200, 240],
            window="hann", ):
        """Initialize Multi resolution STFT loss module.
        Parameters
        ----------
        fft_sizes : list
            List of FFT sizes.
        hop_sizes : list
            List of hop sizes.
        win_lengths : list
            List of window lengths.
        window : str
            Window function type.
        """
        super().__init__()
        assert len(fft_sizes) == len(hop_sizes) == len(win_lengths)
        self.stft_losses = nn.LayerList()
        for fs, ss, wl in zip(fft_sizes, hop_sizes, win_lengths):
            self.stft_losses.append(STFTLoss(fs, ss, wl, window))

    def forward(self, x, y):
        """Calculate forward propagation.
        Parameters
        ----------
        x : Tensor
            Predicted signal (B, T) or (B, #subband, T).
        y : Tensor
            Groundtruth signal (B, T) or (B, #subband, T).
        Returns
        ----------
        Tensor
            Multi resolution spectral convergence loss value.
        Tensor
            Multi resolution log STFT magnitude loss value.
        """
        if len(x.shape) == 3:
            # (B, C, T) -> (B x C, T)
            x = x.reshape([-1, x.shape[2]])
            # (B, C, T) -> (B x C, T)
            y = y.reshape([-1, y.shape[2]])
        sc_loss = 0.0
        mag_loss = 0.0
        for f in self.stft_losses:
            sc_l, mag_l = f(x, y)
            sc_loss += sc_l
            mag_loss += mag_l
        sc_loss /= len(self.stft_losses)
        mag_loss /= len(self.stft_losses)

        return sc_loss, mag_loss


class GeneratorAdversarialLoss(nn.Layer):
    """Generator adversarial loss module."""

    def __init__(
            self,
            average_by_discriminators=True,
            loss_type="mse", ):
        """Initialize GeneratorAversarialLoss module."""
        super().__init__()
        self.average_by_discriminators = average_by_discriminators
        assert loss_type in ["mse", "hinge"], f"{loss_type} is not supported."
        if loss_type == "mse":
            self.criterion = self._mse_loss
        else:
            self.criterion = self._hinge_loss

    def forward(self, outputs):
        """Calcualate generator adversarial loss.
        Parameters
        ----------
        outputs: Tensor or List
        Discriminator outputs or list of discriminator outputs.
        Returns
        ----------
        Tensor
            Generator adversarial loss value.
        """
        if isinstance(outputs, (tuple, list)):
            adv_loss = 0.0
            for i, outputs_ in enumerate(outputs):
                if isinstance(outputs_, (tuple, list)):
                    # case including feature maps
                    outputs_ = outputs_[-1]
                adv_loss += self.criterion(outputs_)
            if self.average_by_discriminators:
                adv_loss /= i + 1
        else:
            adv_loss = self.criterion(outputs)

        return adv_loss

    def _mse_loss(self, x):
        return F.mse_loss(x, paddle.ones_like(x))

    def _hinge_loss(self, x):
        return -x.mean()


class DiscriminatorAdversarialLoss(nn.Layer):
    """Discriminator adversarial loss module."""

    def __init__(
            self,
            average_by_discriminators=True,
            loss_type="mse", ):
        """Initialize DiscriminatorAversarialLoss module."""
        super().__init__()
        self.average_by_discriminators = average_by_discriminators
        assert loss_type in ["mse"], f"{loss_type} is not supported."
        if loss_type == "mse":
            self.fake_criterion = self._mse_fake_loss
            self.real_criterion = self._mse_real_loss

    def forward(self, outputs_hat, outputs):
        """Calcualate discriminator adversarial loss.
        Parameters
        ----------
        outputs_hat : Tensor or list
            Discriminator outputs or list of
            discriminator outputs calculated from generator outputs.
        outputs : Tensor or list
            Discriminator outputs or list of
            discriminator outputs calculated from groundtruth.
        Returns
        ----------
        Tensor
            Discriminator real loss value.
        Tensor
            Discriminator fake loss value.
        """
        if isinstance(outputs, (tuple, list)):
            real_loss = 0.0
            fake_loss = 0.0
            for i, (outputs_hat_,
                    outputs_) in enumerate(zip(outputs_hat, outputs)):
                if isinstance(outputs_hat_, (tuple, list)):
                    # case including feature maps
                    outputs_hat_ = outputs_hat_[-1]
                    outputs_ = outputs_[-1]
                real_loss += self.real_criterion(outputs_)
                fake_loss += self.fake_criterion(outputs_hat_)
            if self.average_by_discriminators:
                fake_loss /= i + 1
                real_loss /= i + 1
        else:
            real_loss = self.real_criterion(outputs)
            fake_loss = self.fake_criterion(outputs_hat)

        return real_loss, fake_loss

    def _mse_real_loss(self, x):
        return F.mse_loss(x, paddle.ones_like(x))

    def _mse_fake_loss(self, x):
        return F.mse_loss(x, paddle.zeros_like(x))


# Losses for SpeedySpeech
# Structural Similarity Index Measure (SSIM)
def gaussian(window_size, sigma):
    gauss = paddle.to_tensor([
        math.exp(-(x - window_size // 2)**2 / float(2 * sigma**2))
        for x in range(window_size)
    ])
    return gauss / gauss.sum()


def create_window(window_size, channel):
    _1D_window = gaussian(window_size, 1.5).unsqueeze(1)
    _2D_window = paddle.matmul(_1D_window, paddle.transpose(
        _1D_window, [1, 0])).unsqueeze([0, 1])
    window = paddle.expand(_2D_window, [channel, 1, window_size, window_size])
    return window


def _ssim(img1, img2, window, window_size, channel, size_average=True):
    mu1 = F.conv2d(img1, window, padding=window_size // 2, groups=channel)
    mu2 = F.conv2d(img2, window, padding=window_size // 2, groups=channel)

    mu1_sq = mu1.pow(2)
    mu2_sq = mu2.pow(2)
    mu1_mu2 = mu1 * mu2

    sigma1_sq = F.conv2d(
        img1 * img1, window, padding=window_size // 2, groups=channel) - mu1_sq
    sigma2_sq = F.conv2d(
        img2 * img2, window, padding=window_size // 2, groups=channel) - mu2_sq
    sigma12 = F.conv2d(
        img1 * img2, window, padding=window_size // 2, groups=channel) - mu1_mu2

    C1 = 0.01**2
    C2 = 0.03**2

    ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) \
             / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))

    if size_average:
        return ssim_map.mean()
    else:
        return ssim_map.mean(1).mean(1).mean(1)


def ssim(img1, img2, window_size=11, size_average=True):
    (_, channel, _, _) = img1.shape
    window = create_window(window_size, channel)
    return _ssim(img1, img2, window, window_size, channel, size_average)


def weighted_mean(input, weight):
    """Weighted mean. It can also be used as masked mean.

    Parameters
    -----------
    input : Tensor 
        The input tensor.
    weight : Tensor
        The weight tensor with broadcastable shape with the input.

    Returns
    ----------
    Tensor [shape=(1,)]
        Weighted mean tensor with the same dtype as input.
    """
    weight = paddle.cast(weight, input.dtype)
    # paddle.Tensor.size is different with torch.size() and has been overrided in s2t.__init__
    broadcast_ratio = input.numel() / weight.numel()
    return paddle.sum(input * weight) / (paddle.sum(weight) * broadcast_ratio)


def masked_l1_loss(prediction, target, mask):
    """Compute maksed L1 loss.

    Parameters
    ----------
    prediction : Tensor
        The prediction.
    target : Tensor
        The target. The shape should be broadcastable to ``prediction``.
    mask : Tensor
        The mask. The shape should be broadcatable to the broadcasted shape of
        ``prediction`` and ``target``.

    Returns
    -------
    Tensor [shape=(1,)]
        The masked L1 loss.
    """
    abs_error = F.l1_loss(prediction, target, reduction='none')
    loss = weighted_mean(abs_error, mask)
    return loss


class MelSpectrogram(nn.Layer):
    """Calculate Mel-spectrogram."""

    def __init__(
            self,
            fs=22050,
            fft_size=1024,
            hop_size=256,
            win_length=None,
            window="hann",
            num_mels=80,
            fmin=80,
            fmax=7600,
            center=True,
            normalized=False,
            onesided=True,
            eps=1e-10,
            log_base=10.0, ):
        """Initialize MelSpectrogram module."""
        super().__init__()
        self.fft_size = fft_size
        if win_length is None:
            self.win_length = fft_size
        else:
            self.win_length = win_length
        self.hop_size = hop_size
        self.center = center
        self.normalized = normalized
        self.onesided = onesided

        if window is not None and not hasattr(signal.windows, f"{window}"):
            raise ValueError(f"{window} window is not implemented")
        self.window = window
        self.eps = eps

        fmin = 0 if fmin is None else fmin
        fmax = fs / 2 if fmax is None else fmax
        melmat = librosa.filters.mel(
            sr=fs,
            n_fft=fft_size,
            n_mels=num_mels,
            fmin=fmin,
            fmax=fmax, )

        self.melmat = paddle.to_tensor(melmat.T)
        self.stft_params = {
            "n_fft": self.fft_size,
            "win_length": self.win_length,
            "hop_length": self.hop_size,
            "center": self.center,
            "normalized": self.normalized,
            "onesided": self.onesided,
        }

        self.log_base = log_base
        if self.log_base is None:
            self.log = paddle.log
        elif self.log_base == 2.0:
            self.log = paddle.log2
        elif self.log_base == 10.0:
            self.log = paddle.log10
        else:
            raise ValueError(f"log_base: {log_base} is not supported.")

    def forward(self, x):
        """Calculate Mel-spectrogram.
        Parameters
        ----------
        x : Tensor
            Input waveform tensor (B, T) or (B, 1, T).
        Returns
        ----------
        Tensor
            Mel-spectrogram (B, #mels, #frames).
        """
        if len(x.shape) == 3:
            # (B, C, T) -> (B*C, T)
            x = x.reshape([-1, paddle.shape(x)[2]])

        if self.window is not None:
            # calculate window
            window = signal.get_window(
                self.window, self.win_length, fftbins=True)
            window = paddle.to_tensor(window)
        else:
            window = None

        x_stft = paddle.signal.stft(x, window=window, **self.stft_params)
        real = x_stft.real()
        imag = x_stft.imag()
        # (B, #freqs, #frames) -> (B, $frames, #freqs)
        real = real.transpose([0, 2, 1])
        imag = imag.transpose([0, 2, 1])
        x_power = real**2 + imag**2
        x_amp = paddle.sqrt(paddle.clip(x_power, min=self.eps))
        x_mel = paddle.matmul(x_amp, self.melmat)
        x_mel = paddle.clip(x_mel, min=self.eps)

        return self.log(x_mel).transpose([0, 2, 1])


class MelSpectrogramLoss(nn.Layer):
    """Mel-spectrogram loss."""

    def __init__(
            self,
            fs=22050,
            fft_size=1024,
            hop_size=256,
            win_length=None,
            window="hann",
            num_mels=80,
            fmin=80,
            fmax=7600,
            center=True,
            normalized=False,
            onesided=True,
            eps=1e-10,
            log_base=10.0, ):
        """Initialize Mel-spectrogram loss."""
        super().__init__()
        self.mel_spectrogram = MelSpectrogram(
            fs=fs,
            fft_size=fft_size,
            hop_size=hop_size,
            win_length=win_length,
            window=window,
            num_mels=num_mels,
            fmin=fmin,
            fmax=fmax,
            center=center,
            normalized=normalized,
            onesided=onesided,
            eps=eps,
            log_base=log_base, )

    def forward(self, y_hat, y):
        """Calculate Mel-spectrogram loss.
        Parameters
        ----------
        y_hat : Tensor
            Generated single tensor (B, 1, T).
        y : Tensor
            Groundtruth single tensor (B, 1, T).
        Returns
        ----------
        Tensor
            Mel-spectrogram loss value.
        """
        mel_hat = self.mel_spectrogram(y_hat)
        mel = self.mel_spectrogram(y)
        mel_loss = F.l1_loss(mel_hat, mel)

        return mel_loss


class FeatureMatchLoss(nn.Layer):
    """Feature matching loss module."""

    def __init__(
            self,
            average_by_layers=True,
            average_by_discriminators=True,
            include_final_outputs=False, ):
        """Initialize FeatureMatchLoss module."""
        super().__init__()
        self.average_by_layers = average_by_layers
        self.average_by_discriminators = average_by_discriminators
        self.include_final_outputs = include_final_outputs

    def forward(self, feats_hat, feats):
        """Calcualate feature matching loss.
        Parameters
        ----------
        feats_hat : list
            List of list of discriminator outputs
            calcuated from generater outputs.
        feats : list
            List of list of discriminator outputs
            calcuated from groundtruth.
        Returns
        ----------
        Tensor
            Feature matching loss value.

        """
        feat_match_loss = 0.0
        for i, (feats_hat_, feats_) in enumerate(zip(feats_hat, feats)):
            feat_match_loss_ = 0.0
            if not self.include_final_outputs:
                feats_hat_ = feats_hat_[:-1]
                feats_ = feats_[:-1]
            for j, (feat_hat_, feat_) in enumerate(zip(feats_hat_, feats_)):
                feat_match_loss_ += F.l1_loss(feat_hat_, feat_.detach())
            if self.average_by_layers:
                feat_match_loss_ /= j + 1
            feat_match_loss += feat_match_loss_
        if self.average_by_discriminators:
            feat_match_loss /= i + 1

        return feat_match_loss
