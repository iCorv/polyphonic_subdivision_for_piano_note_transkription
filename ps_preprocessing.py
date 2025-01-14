"""Shared methods to provide data in the tfrecords format to the transcription model.

    frame: An individual row of a spectrogram computed from some
           number of audio samples.
    example: An individual training example. The number of frames in an example
             is determined by the context length, before and after the center frame.

    Some methods are adapted from:
    @inproceedings{kelz_icassp18, author = {Rainer Kelz and Gerhard Widmer},
    title = {Investigating Label Noise Sensitivity Of Convolutional Neural Networks For Fine Grained Audio Signal Labelling}
    booktitle = {2018 {IEEE} International Conference on Acoustics, Speech and Signal Processing, {ICASSP} 2018, Calgary,
    Alberta, Canada, April 15-20, 2018}, year = {2018} }
    https://github.com/rainerkelz/ICASSP18
"""

import numpy as np
import madmom
import tensorflow as tf
import os
import configurations.ps_preprocessing_parameters as ppp
import warnings
import pretty_midi
import jams
import glob
from joblib import Parallel, delayed
import multiprocessing
from madmom.io import midi
from enum import Enum
warnings.filterwarnings("ignore")


class Fold(Enum):
    """Distinguish the different folds the model is trained on."""
    fold_1 = 0
    fold_2 = 1
    fold_3 = 2
    fold_4 = 3
    fold_benchmark = 4
    fold_single_note = 5


def wav_to_spec(base_dir, filename, _audio_options):
    """Transforms the contents of a wav file into a series of spec frames."""
    audio_filename = os.path.join(base_dir, filename + '.wav')

    spec_type, audio_options = get_spec_processor(_audio_options, madmom.audio.spectrogram)


    spectrogram = spec_type(audio_filename, **audio_options)
    #comb = spectrogram
    superflux_proc = madmom.audio.spectrogram.SpectrogramDifferenceProcessor(diff_max_bins=3)
    superflux_freq = superflux_proc(spectrogram.T)
    superflux_freq = superflux_freq.T

    superflux_time = superflux_proc(spectrogram)
    # it's necessary to cast this to np.array, b/c the madmom-class holds references to way too much memory
    comb = np.array(spectrogram + superflux_time + superflux_freq)
    comb = comb / np.max(np.max(comb))
    comb = np.clip(comb, a_min=0.001, a_max=1.0)
    return comb


def wav_to_hpcp(base_dir, filename):
    """Transforms the contents of a wav file into a series of octave-wise HPCP frames."""
    audio_filename = os.path.join(base_dir, filename + '.wav')
    audio_options = ppp.get_hpcp_parameters()
    fmin = audio_options['fmin']
    fmax = audio_options['fmax']
    hpcp_processor = getattr(madmom.audio.chroma, 'HarmonicPitchClassProfile')
    audio_options['fmin'] = fmin[0]
    audio_options['fmax'] = fmax[0]
    hpcp = np.array(hpcp_processor(audio_filename, **audio_options))

    for index in range(1, len(fmin)-1):
        audio_options['fmin'] = fmin[index]
        audio_options['fmax'] = fmax[index]
        hpcp = np.append(hpcp, np.array(hpcp_processor(audio_filename, **audio_options)), axis=1)
    audio_options['fmin'] = fmin[-1]
    audio_options['fmax'] = fmax[-1]
    # last octave is only used up to 12/3
    hpcp = np.append(hpcp, np.array(hpcp_processor(audio_filename, **audio_options)[:, :int(audio_options['num_classes']
                                                                                            / 3)]), axis=1)
    # post-processing,
    # normalize hpcp by max value per frame. Add a small value to avoid division by zero
    # norm_vec = np.max(hpcp, axis=1) + 1e-7
    # hpcp = hpcp/norm_vec[:, None]

    hpcp = np.log10(hpcp + 1.0)

    hpcp = hpcp/np.max(hpcp)
    return hpcp


def get_spec_processor(_audio_options, madmom_spec):
    """Returns the madmom spectrogram processor as defined in audio options."""
    audio_options = dict(_audio_options)

    if 'spectrogram_type' in audio_options:
        spectype = getattr(madmom_spec, audio_options['spectrogram_type'])
        del audio_options['spectrogram_type']
    else:
        spectype = getattr(madmom_spec, 'LogarithmicFilteredSpectrogram')

    if 'filterbank' in audio_options:
        audio_options['filterbank'] = getattr(madmom_spec, audio_options['filterbank'])
    else:
        audio_options['filterbank'] = getattr(madmom_spec, 'LogarithmicFilterbank')

    return spectype, audio_options


def midi_to_triple_groundtruth(base_dir, filename, dt, n_frames, n_onset_plus):
    """Computes the frame-wise ground truth from a piano midi file as a note vector. For frame, onset and offset"""
    midi_filename = os.path.join(base_dir, filename + '.mid')
    notes = midi.load_midi(midi_filename)
    frame_gt = np.zeros((n_frames, 88)).astype(np.int64)
    onset_gt = np.zeros((n_frames, 88)).astype(np.int64)
    onset_plus = []
    for index in range(0, n_onset_plus):
        onset_plus.append(np.zeros((n_frames, 88)).astype(np.int64))

    offset_gt = np.zeros((n_frames, 88)).astype(np.int64)
    for onset, _pitch, duration, velocity, _channel in notes:
        pitch = int(_pitch)
        frame_start = int(np.round(onset / dt))
        frame_end = int(np.round((onset + duration) / dt))
        label = pitch - 21
        frame_gt[frame_start:frame_end, label] = 1
        onset_gt[frame_start, label] = 1
        for index in range(0, n_onset_plus):
            if frame_start + index + 1 < frame_end:
                onset_plus[index][frame_start + index + 1, label] = 1

        offset_gt[frame_end, label] = 1
    return frame_gt, onset_gt, offset_gt, onset_plus

def midi_to_groundtruth(base_dir, filename, dt, n_frames, is_chroma=False):
    """Computes the frame-wise ground truth from a piano midi file, as a note or chroma vector."""
    midi_filename = os.path.join(base_dir, filename + '.mid')
    notes = midi.load_midi(midi_filename)
    ground_truth = np.zeros((n_frames, 12 if is_chroma else 88)).astype(np.int64)
    onset_gt = np.zeros((n_frames, 88)).astype(np.int64)
    for onset, _pitch, duration, velocity, _channel in notes:
        pitch = int(_pitch)
        frame_start = int(np.round(onset / dt))
        frame_end = int(np.round((onset + duration) / dt))
        label = np.mod(pitch - 21, 12) if is_chroma else pitch - 21
        ground_truth[frame_start:frame_end, label] = 1
        if frame_start+2 <= frame_end:
            onset_gt[frame_start:frame_start+2, label] = 1
    return ground_truth, onset_gt


def jams_to_midi(filepath, q=1):
    # q = 1: with pitch bend. q = 0: without pitch bend.
    jam = jams.load(filepath)
    midi = pretty_midi.PrettyMIDI()
    annos = jam.search(namespace='note_midi')
    if len(annos) == 0:
        annos = jam.search(namespace='pitch_midi')
    for anno in annos:
        midi_ch = pretty_midi.Instrument(program=25)
        for note in anno:
            pitch = int(round(note.value))
            bend_amount = int(round((note.value - pitch) * 4096))
            st = note.time
            dur = note.duration
            n = pretty_midi.Note(
                velocity=100 + np.random.choice(range(-5, 5)),
                pitch=pitch, start=st,
                end=st + dur
            )
            pb = pretty_midi.PitchBend(pitch=bend_amount * q, time=st)
            midi_ch.notes.append(n)
            midi_ch.pitch_bends.append(pb)
        if len(midi_ch.notes) != 0:
            midi.instruments.append(midi_ch)
    return midi


def convert_jams_to_midi(folder, q=1):
    files = [name for name in os.listdir(folder) if name.endswith(".jams")]
    for filepath in files:
        midi_filepath = filepath.split(".")[0]
        midi_filepath = os.path.join(folder, midi_filepath + ".mid")
        filepath = os.path.join(folder, filepath)
        midi = jams_to_midi(filepath, q)
        midi.write(midi_filepath)


def _float_feature(value):
    """Converts a value to a tensorflow feature for float data types."""
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))


def _int64_feature(value):
    """Converts a value to a tensorflow feature for int data types."""
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _bytes_feature(value):
    """Converts a value to a tensorflow feature for byte data types."""
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def preprocess_fold(fold, mode, norm=False):
    """Preprocess an entire fold as defined in the preprocessing parameters.
        fold - Fold.fold_1, Fold.fold_2, Fold.fold_3, Fold.fold_4, Fold.fold_benchmark
        mode - 'train', 'valid' or 'test' to address the correct config parameter
    """
    config = ppp.get_preprocessing_parameters(fold.value)
    audio_config = config['audio_config']

    # load fold
    filenames = open(config[mode+'_fold'], 'r').readlines()
    filenames = [f.strip() for f in filenames]

    total_examples_processed = 0

    for file in filenames:
        # split file path string at "/" and take the last split, since it's the actual filename
        num_ex_processed = write_file_to_tfrecords(config['tfrecords_'+mode+'_fold'] + file.split('/')[-1] +
                                                   ".tfrecords", config['audio_path'], file, audio_config, norm,
                                                   config['context_frames'], config['is_hpcp'])
        total_examples_processed = total_examples_processed + num_ex_processed

    print("Examples processed: " + str(total_examples_processed))
    np.savez(config['tfrecords_' + mode + '_fold'] + "total_examples_processed",
             total_examples_processed=total_examples_processed)


def preprocess_fold_parallel(fold, mode, norm=False):
    """Parallel preprocess an entire fold as defined in the preprocessing parameters.
        This seems only to work on Win with Anaconda!
        fold - Fold.fold_1, Fold.fold_2, Fold.fold_3, Fold.fold_4, Fold.fold_benchmark
        mode - 'train', 'valid' or 'test' to address the correct config parameter
    """
    config = ppp.get_preprocessing_parameters(fold.value)
    audio_config = config['audio_config']

    # load fold
    filenames = open(config[mode+'_fold'], 'r').readlines()
    filenames = [f.strip() for f in filenames]

    def parallel_loop(file):
        # split file path string at "/" and take the last split, since it's the actual filename
        num_ex_processed = write_file_to_tfrecords(config['tfrecords_'+mode+'_fold'] + file.split('/')[-1] +
                                                   ".tfrecords", config['audio_path'], file, audio_config, norm,
                                                   config['context_frames'], config['is_hpcp'])
        return num_ex_processed

    num_cores = multiprocessing.cpu_count()

    total_examples_processed = Parallel(n_jobs=num_cores)(delayed(parallel_loop)(file) for file in filenames)
    print("Examples processed: " + str(np.sum(total_examples_processed)))
    np.savez(config['tfrecords_' + mode + '_fold'] + "total_examples_processed",
             total_examples_processed=np.sum(total_examples_processed))


def preprocess_non_overlap_fold_parallel(fold, mode, norm=False):
    """Parallel preprocess an entire fold as defined in the preprocessing parameters.
        This seems only to work on Win with Anaconda!
        fold - Fold.fold_1, Fold.fold_2, Fold.fold_3, Fold.fold_4, Fold.fold_benchmark
        mode - 'train', 'valid' or 'test' to address the correct config parameter
    """
    config = ppp.get_preprocessing_parameters(fold.value)
    audio_config = config['audio_config']

    # load fold
    filenames = open(config[mode+'_fold'], 'r').readlines()
    filenames = [f.strip() for f in filenames]

    def parallel_loop(file):
        # split file path string at "/" and take the last split, since it's the actual filename
        num_ex_processed = write_file_to_non_overlap_tfrecords(config['tfrecords_'+mode+'_fold'] + file.split('/')[-1] +
                                                   ".tfrecords", config['audio_path'], file, audio_config, norm,
                                                   config['context_frames'], config['is_hpcp'])
        return num_ex_processed

    num_cores = multiprocessing.cpu_count()

    total_examples_processed = Parallel(n_jobs=num_cores)(delayed(parallel_loop)(file) for file in filenames)
    print("Examples processed: " + str(np.sum(total_examples_processed)))
    np.savez(config['tfrecords_' + mode + '_fold'] + "total_examples_processed",
             total_examples_processed=np.sum(total_examples_processed))


def preprocess_non_overlap_fold(fold, mode, norm=False):
    """Preprocess an entire fold as defined in the preprocessing parameters.
        fold - Fold.fold_1, Fold.fold_2, Fold.fold_3, Fold.fold_4, Fold.fold_benchmark
        mode - 'train', 'valid' or 'test' to address the correct config parameter
    """
    config = ppp.get_preprocessing_parameters(fold.value)
    audio_config = config['audio_config']

    # load fold
    filenames = open(config[mode+'_fold'], 'r').readlines()
    filenames = [f.strip() for f in filenames]

    total_examples_processed = 0

    for file in filenames:
        # split file path string at "/" and take the last split, since it's the actual filename
        num_ex_processed = write_file_to_non_overlap_tfrecords(config['tfrecords_'+mode+'_fold'] + file.split('/')[-1] +
                                                   ".tfrecords", config['audio_path'], file, audio_config, norm,
                                                   config['context_frames'], config['is_hpcp'])
        total_examples_processed = total_examples_processed + num_ex_processed

    print("Examples processed: " + str(total_examples_processed))
    np.savez(config['tfrecords_' + mode + '_fold'] + "total_examples_processed",
             total_examples_processed=total_examples_processed)


def write_file_to_tfrecords(write_file, base_dir, read_file, audio_config, norm, context_frames, is_hpcp):
    """Transforms a wav and mid file to features and writes them to a tfrecords file."""
    writer = tf.python_io.TFRecordWriter(write_file)
    if is_hpcp:
        spectrogram = wav_to_hpcp(base_dir, read_file)
    else:
        spectrogram = wav_to_spec(base_dir, read_file, audio_config)

    print(spectrogram.shape)
    ground_truth = midi_to_groundtruth(base_dir, read_file, 1. / audio_config['fps'], spectrogram.shape[0])
    total_examples_processed = 0
    # re-scale spectrogram to the range [0, 1]
    if norm:
        spectrogram = np.divide(spectrogram, np.max(spectrogram))

    for frame in range(context_frames, spectrogram.shape[0] - context_frames):
        example = features_to_example(spectrogram[frame - context_frames:frame + context_frames + 1, :],
                                      ground_truth[frame, :])

        # Serialize to string and write on the file
        writer.write(example.SerializeToString())
        total_examples_processed = total_examples_processed + 1

    writer.close()
    return total_examples_processed


def chunks(sequence, length):
    for index in range(0, len(sequence), length):
        yield sequence[index:index + length]


def write_file_to_non_overlap_tfrecords(write_file, base_dir, read_file, audio_config, norm, context_frames, is_hpcp):
    """Transforms a wav and mid file to features and writes them to a tfrecords file."""
    writer = tf.python_io.TFRecordWriter(write_file)
    if is_hpcp:
        spectrogram = wav_to_hpcp(base_dir, read_file)
    else:
        spectrogram = wav_to_spec(base_dir, read_file, audio_config)

    print(spectrogram.shape)
    ground_truth, onset_gt = midi_to_groundtruth(base_dir, read_file, 1. / audio_config['fps'], spectrogram.shape[0])
    total_examples_processed = 0
    # re-scale spectrogram to the range [0, 1]
    if norm:
        spectrogram = np.divide(spectrogram, np.max(spectrogram))

    split_spec = list(chunks(spectrogram, context_frames))
    split_gt = list(chunks(ground_truth, context_frames))
    split_onset_gt = list(chunks(onset_gt, context_frames))

    split_spec[-1] = np.append(split_spec[-1], np.zeros([context_frames - split_spec[-1].shape[0], split_spec[-1].shape[1]]),
                               axis=0)
    split_gt[-1] = np.append(split_gt[-1], np.zeros([context_frames - split_gt[-1].shape[0], split_gt[-1].shape[1]],
                                                    dtype=np.int64), axis=0)
    split_onset_gt[-1] = np.append(split_onset_gt[-1], np.zeros([context_frames - split_onset_gt[-1].shape[0],
                                                                 split_onset_gt[-1].shape[1]], dtype=np.int64), axis=0)

    for ex, gt, onset in zip(split_spec, split_gt, split_onset_gt):
        example = features_to_non_overlap_multi_head_example(ex, gt, onset)

        # Serialize to string and write on the file
        writer.write(example.SerializeToString())
        total_examples_processed = total_examples_processed + 1

    writer.close()
    return total_examples_processed


def features_to_example(spectrogram, ground_truth):
    """Build an example from spectrogram and ground truth data."""
    # Create a feature
    feature = {"label": _int64_feature(ground_truth),
               "spec": _float_feature(spectrogram.ravel())}

    # Create an example protocol buffer
    example = tf.train.Example(features=tf.train.Features(feature=feature))
    return example


def features_to_non_overlap_example(spectrogram, ground_truth):
    """Build an example from spectrogram and ground truth data."""
    # Create a feature
    feature = {"label": _int64_feature(ground_truth.ravel()),
               "spec": _float_feature(spectrogram.ravel())}

    # Create an example protocol buffer
    example = tf.train.Example(features=tf.train.Features(feature=feature))
    return example


def features_to_non_overlap_multi_head_example(spectrogram, ground_truth, onset):
    """Build an example from spectrogram and ground truth data."""
    # Create a feature
    feature = {"label": _int64_feature(ground_truth.ravel()),
               "onset": _int64_feature(onset.ravel()),
               "spec": _float_feature(spectrogram.ravel())}

    # Create an example protocol buffer
    example = tf.train.Example(features=tf.train.Features(feature=feature))
    return example