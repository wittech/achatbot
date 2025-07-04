# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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
# Reference: https://github.com/openai/whisper/blob/main/whisper/audio.py
import os
from typing import Union
from functools import lru_cache

import numpy as np
import torch
import torch.nn.functional as F


@lru_cache(maxsize=None)
def mel_filters(device, n_mels: int = 128, mel_filters_dir: str = None) -> torch.Tensor:
    """
    load the mel filterbank matrix for projecting STFT into a Mel spectrogram.
    Allows decoupling librosa dependency; saved using:

        np.savez_compressed(
            "mel_filters.npz",
            mel_128=librosa.filters.mel(sr=16000, n_fft=400, n_mels=128),
        )
    """
    assert n_mels == 80 or n_mels == 128, f"Unsupported n_mels: {n_mels}"
    if mel_filters_dir is None:
        mel_filters_path = os.path.join(os.path.dirname(__file__), "mel_filters.npz")
    else:
        mel_filters_path = os.path.join(mel_filters_dir, "mel_filters.npz")
    with np.load(mel_filters_path) as f:
        return torch.from_numpy(f[f"mel_{n_mels}"]).to(device)


def log_mel_spectrogram(
    audio: Union[torch.Tensor],
    filters: torch.Tensor,
    n_fft: int = 400,
    hop_length: int = 160,
):
    """
    Compute the log-Mel spectrogram of

    Parameters
    ----------
    audio: Union[str, np.ndarray, torch.Tensor], shape = (*)
        The path to audio or either a NumPy array or Tensor containing the audio waveform in 16 kHz

    n_mels: int
        The number of Mel-frequency filters, only 80 or 128 is supported

    filters: torch.Tensor

    Returns
    -------
    torch.Tensor, shape = (128, n_frames)
        A Tensor that contains the Mel spectrogram
    """
    window = torch.hann_window(n_fft).to(audio.device)
    stft = torch.stft(audio, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    mel_spec = filters @ magnitudes
    log_spec = torch.clamp(mel_spec, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    # cast to float 16
    log_spec = log_spec.half()
    return log_spec


class FeatureExtractor(torch.nn.Module):
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    def __init__(self, n_mels: int = 128, mel_filters_dir: str = None):
        self.device = torch.device("cuda")
        self.n_mels = n_mels
        self.mel_filters_dir = mel_filters_dir
        self.filters = mel_filters(
            self.device,
            n_mels=self.n_mels,
            mel_filters_dir=mel_filters_dir,
        )

    def compute_feature(self, wav, padding_target_len):
        """
        Compute the log-Mel spectrogram of the input audio waveform.
        mel: [1, feature_dim, seq_len]
        """
        mel = log_mel_spectrogram(wav, self.filters)
        # don't do this, need move out of this function
        # assert padding_target_len <= 3000, f"padding must be less than 3000, got {padding_target_len}"
        if mel.shape[1] < padding_target_len:
            mel = F.pad(mel, (0, padding_target_len - mel.shape[1]), mode="constant")
        if mel.shape[1] % 2:
            # pad to even length for remove_padding case, since conv1d requires even length
            mel = torch.nn.functional.pad(mel, (0, 1))
        mel = mel.unsqueeze(0)
        return mel
