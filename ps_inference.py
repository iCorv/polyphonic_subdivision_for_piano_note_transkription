import tensorflow as tf
import ps_model
import configurations.ps_hyper_parameters as php
import numpy as np
import ps_preprocessing as prep
import configurations.ps_preprocessing_parameters as ppp
import ps_utility as util
import madmom
from tqdm import tqdm
import os
import mir_eval
from scipy.io import savemat
from scipy import signal


def convert_fold_to_note_activation(fold, mode, net, model_dir, save_dir, norm=False):
    """Preprocess an entire fold as defined in the preprocessing parameters and classify its note activations.
        fold - Fold.fold_1, Fold.fold_2, Fold.fold_3, Fold.fold_4, Fold.fold_benchmark
        mode - 'train', 'valid' or 'test' to address the correct config parameter
        norm - Flag if the spectrogram should be normed to 1
        net - The network used for classification, e.g. 'ConvNet, 'ResNet_v1'
        model_dir - e.g. "./model_ResNet_fold_4/". For a specific checkpoint, change the checkpoint number in the
        chekpoint file from the model folder.
        save_dir - folder were to save note activations, e.g. "./note_activations/"
    """
    config = ppp.get_preprocessing_parameters(fold.value)
    audio_config = config['audio_config']

    # load fold
    filenames = open(config[mode + '_fold'], 'r').readlines()
    filenames = [f.strip() for f in filenames]

    predictor = build_predictor(net, model_dir)

    for file in filenames:
        # split file path string at "/" and take the last split, since it's the actual filename
        write_note_activation_to_mat(save_dir + file.split('/')[-1], config['audio_path'], file,
                                     audio_config, norm, config['context_frames'], predictor)


def write_note_activation_to_mat(write_file, base_dir, read_file, audio_config, norm, context_frames, predictor):
    """Transforms a wav and mid file to features and writes them to a tfrecords file."""
    spectrogram = prep.wav_to_spec(base_dir, read_file, audio_config)
    ground_truth = prep.midi_to_groundtruth(base_dir, read_file, 1. / audio_config['fps'], spectrogram.shape[0])
    # re-scale spectrogram to the range [0, 1]
    if norm:
        spectrogram = np.divide(spectrogram, np.max(spectrogram))
    note_activation = spectrogram_to_note_activation(spectrogram, context_frames, predictor)

    savemat(write_file, {"features": note_activation, "labels": ground_truth})


def get_note_activation(base_dir, read_file, audio_config, norm, context_frames, predictor, n_onset_plus, is_hpcp=False, use_rnn=False, offset=0):
    """Transforms a wav and mid file to features and writes them to a tfrecords file."""

    if is_hpcp:
        spectrogram = prep.wav_to_hpcp(base_dir, read_file)
    else:
        spectrogram = prep.wav_to_spec(base_dir, read_file, audio_config)

    gt_frame, \
    gt_onset, \
    gt_offset, \
    onset_plus = prep.midi_to_triple_groundtruth(base_dir, read_file, 1. / audio_config['fps'],
                                                 spectrogram.shape[0], n_onset_plus)
    # re-scale spectrogram to the range [0, 1]
    if norm:
        spectrogram = np.divide(spectrogram, np.max(spectrogram))

    #note_activation = spectrogram_to_note_activation(spectrogram, context_frames, predictor)

    # get note activation fn from model
    if use_rnn:
        note_activation, onset_activation = spectrogram_to_non_overlap_note_activation(spectrogram, 2000, predictor)
    else:
        note_activation = spectrogram_to_note_activation(spectrogram, context_frames, predictor)

    return note_activation, onset_activation, gt_frame, gt_onset, gt_offset, onset_plus


def compute_all_error_metrics(fold, mode, net, model_dir, save_dir, save_file, norm=False, n_onset_plus=25, offset=0):
    """Error metrics for an entire fold as defined in the preprocessing parameters.
        fold - Fold.fold_1, Fold.fold_2, Fold.fold_3, Fold.fold_4, Fold.fold_benchmark
        mode - 'train', 'valid' or 'test' to address the correct config parameter
        norm - Flag if the spectrogram should be normed to 1
        net - The network used for classification, e.g. 'ConvNet, 'ResNet_v1'
        model_dir - e.g. "./model_ResNet_fold_4/". For a specific checkpoint, change the checkpoint number in the
        chekpoint file from the model folder.
        save_dir - folder were to save note activations, e.g. "./note_activations/"
        save_file - name of the save file which ends with .txt
    """
    config = ppp.get_preprocessing_parameters(fold.value)
    audio_config = config['audio_config']

    # load fold
    filenames = open(config[mode + '_fold'], 'r').readlines()
    filenames = [f.strip() for f in filenames]

    # build predictor
    predictor, hparams = build_predictor(net, model_dir)
    # init madmom peak picker
    # proc = madmom.features.notes.NotePeakPickingProcessor(threshold=0.1, fps=100)
    # init piano note processor for onset prediction
    # rnn_processor = madmom.features.notes.RNNPianoNoteProcessor()

    # init lists
    frame_wise_metrics = []
    frame_wise_metrics_with_onset_pred = []
    frame_wise_metrics_with_onset_pred_heuristic = []
    frame_wise_onset_metrics = []
    frame_wise_onset_plus_metrics = []
    for index in range(0, n_onset_plus):
        frame_wise_onset_plus_metrics.append([])

    frame_wise_offset_metrics = []

    note_wise_onset_metrics = []
    note_wise_onset_offset_metrics = []

    note_wise_onset_metrics_with_onset_pred = []
    note_wise_onset_offset_metrics_with_onset_pred = []
    note_wise_onset_metrics_with_onset_pred_heuristic = []
    note_wise_onset_offset_metrics_with_onset_pred_heuristic = []

    #filenames = filenames[0:3]
    num_pieces = len(filenames)
    onset_duration_heuristic = 10
    for file in tqdm(filenames, total=num_pieces):
        # split file path string at "/" and take the last split, since it's the actual filename
        note_activation, \
        onset_activation, \
        gt_frame, gt_onset, \
        gt_offset, \
        onset_plus = get_note_activation(config['audio_path'], file, audio_config,
                                         norm, config['context_frames'], predictor, n_onset_plus, config['is_hpcp'], use_rnn=hparams['use_rnn'])

        frames = np.greater_equal(note_activation, 0.5)
        # return precision, recall, f-score, accuracy (without TN)
        frame_wise_metrics.append(util.eval_frame_wise(note_activation, gt_frame))
        # multiply note activation with ground truth in order to blend out the rest of the activation fn
        frame_wise_onset_metrics.append(util.eval_frame_wise(np.multiply(note_activation, gt_onset), gt_onset))
        for index in range(0, n_onset_plus):
            frame_wise_onset_plus_metrics[index].append(util.eval_frame_wise(np.multiply(note_activation, onset_plus[index]), onset_plus[index]))

        frame_wise_offset_metrics.append(util.eval_frame_wise(np.multiply(note_activation, gt_offset), gt_offset))

        # rnn_act_fn = rnn_processor(os.path.join(config['audio_path'], file + '_mic.wav'))
        # onset_predictions_timings = proc(rnn_act_fn)
        #
        # onset_predictions = util.piano_roll_rep(onset_frames=(onset_predictions_timings[:, 0] /
        #                                                       (1. / audio_config['fps'])).astype(int),
        #                                         midi_pitches=onset_predictions_timings[:, 1].astype(int) - 21,
        #                                         piano_roll_shape=np.shape(frames))
        #
        #
        # onset_predictions_with_heuristic = util.piano_roll_rep(onset_frames=(onset_predictions_timings[:, 0] /
        #                                                       (1. / audio_config['fps'])).astype(int),
        #                                         midi_pitches=onset_predictions_timings[:, 1].astype(int) - 21,
        #                                         piano_roll_shape=np.shape(frames), onset_duration=onset_duration_heuristic)

        onset_predictions = np.greater_equal(onset_activation, 0.5)

        est_intervals_onset_pred_only, est_pitches_onset_pred_only = util.pianoroll_to_interval_sequence(onset_predictions, frames_per_second=
        audio_config['fps'],
                                            min_midi_pitch=21,
                                            onset_predictions=None,
                                            offset_predictions=None)

        onset_predictions_with_heuristic = util.piano_roll_rep(onset_frames=(est_intervals_onset_pred_only[:,0] /
                                                              (1. / audio_config['fps'])).astype(int),
                                                midi_pitches=est_pitches_onset_pred_only.astype(int) - 21,
                                                piano_roll_shape=np.shape(frames), onset_duration=onset_duration_heuristic)



        frames_with_onset_heuristic = np.logical_or(frames, onset_predictions_with_heuristic)

        frame_wise_metrics_with_onset_pred.append(util.eval_frame_wise(np.logical_or(frames, onset_predictions), gt_frame))
        frame_wise_metrics_with_onset_pred_heuristic.append(util.eval_frame_wise(frames_with_onset_heuristic, gt_frame))

        ref_intervals, ref_pitches = util.pianoroll_to_interval_sequence(gt_frame,
                                                                         frames_per_second=audio_config['fps'],
                                                                         min_midi_pitch=21, onset_predictions=gt_onset,
                                                                         offset_predictions=None)
        est_intervals, est_pitches = util.pianoroll_to_interval_sequence(frames, frames_per_second=audio_config['fps'],
                                                                         min_midi_pitch=21, onset_predictions=None,
                                                                         offset_predictions=None)

        est_intervals_onset_pred, est_pitches_onset_pred = util.pianoroll_to_interval_sequence(frames, frames_per_second=
        audio_config['fps'],
                                                                                              min_midi_pitch=21,
                                                                                              onset_predictions=onset_predictions,
                                                                                              offset_predictions=None)

        est_intervals_onset_pred_heuristic, est_pitches_onset_pred_heuristic = util.pianoroll_to_interval_sequence(frames_with_onset_heuristic, frames_per_second=
        audio_config['fps'],
                                                                                              min_midi_pitch=21,
                                                                                              onset_predictions=onset_predictions,
                                                                                              offset_predictions=None)
        # w/o onset predictions
        # return precision, recall, f-score, overlap_ratio
        note_wise_onset_metrics.append(mir_eval.transcription.precision_recall_f1_overlap(ref_intervals,
                                                                                          util.midi_to_hz(
                                                                                              ref_pitches),
                                                                                          est_intervals,
                                                                                          util.midi_to_hz(
                                                                                              est_pitches),
                                                                                          offset_ratio=None))
        note_wise_onset_offset_metrics.append(mir_eval.transcription.precision_recall_f1_overlap(ref_intervals,
                                                                                                 util.midi_to_hz(
                                                                                                     ref_pitches),
                                                                                                 est_intervals,
                                                                                                 util.midi_to_hz(
                                                                                                     est_pitches)))

        # w/ onset predictions
        # return precision, recall, f-score, overlap_ratio
        note_wise_onset_metrics_with_onset_pred.append(mir_eval.transcription.precision_recall_f1_overlap(ref_intervals,
                                                                                                          util.midi_to_hz(
                                                                                                              ref_pitches),
                                                                                                          est_intervals_onset_pred,
                                                                                                          util.midi_to_hz(
                                                                                                              est_pitches_onset_pred),
                                                                                                          offset_ratio=None))
        note_wise_onset_offset_metrics_with_onset_pred.append(
            mir_eval.transcription.precision_recall_f1_overlap(ref_intervals,
                                                               util.midi_to_hz(
                                                                   ref_pitches),
                                                               est_intervals_onset_pred,
                                                               util.midi_to_hz(
                                                                   est_pitches_onset_pred)))

        # w/ onset predictions and heuristics
        # return precision, recall, f-score, overlap_ratio
        note_wise_onset_metrics_with_onset_pred_heuristic.append(mir_eval.transcription.precision_recall_f1_overlap(ref_intervals,
                                                                                                          util.midi_to_hz(
                                                                                                              ref_pitches),
                                                                                                          est_intervals_onset_pred_heuristic,
                                                                                                          util.midi_to_hz(
                                                                                                              est_pitches_onset_pred_heuristic),
                                                                                                          offset_ratio=None))
        note_wise_onset_offset_metrics_with_onset_pred_heuristic.append(
            mir_eval.transcription.precision_recall_f1_overlap(ref_intervals,
                                                               util.midi_to_hz(
                                                                   ref_pitches),
                                                               est_intervals_onset_pred_heuristic,
                                                               util.midi_to_hz(
                                                                   est_pitches_onset_pred_heuristic)))



    # frame-wise metrics (precision/recall/f1-score
    mean_frame_wise = util.mean_eval_frame_wise(frame_wise_metrics, num_pieces)

    mean_frame_wise_onset = util.mean_eval_frame_wise(frame_wise_onset_metrics, num_pieces)

    mean_frame_wise_with_onset_pred = util.mean_eval_frame_wise(frame_wise_metrics_with_onset_pred, num_pieces)
    mean_frame_wise_with_onset_pred_heuristic = util.mean_eval_frame_wise(frame_wise_metrics_with_onset_pred_heuristic, num_pieces)

    mean_frame_wise_onset_plus = []
    for index in range(0, n_onset_plus):
        mean_frame_wise_onset_plus.append(util.mean_eval_frame_wise(frame_wise_onset_plus_metrics[index], num_pieces))

    mean_frame_wise_offset = util.mean_eval_frame_wise(frame_wise_offset_metrics, num_pieces)

    # note metrics w/o onset predictions (precision/recall/f1-score
    mean_note_wise_onset_metrics = util.mean_eval_frame_wise(note_wise_onset_metrics, num_pieces)

    mean_note_wise_onset_offset_metrics = util.mean_eval_frame_wise(note_wise_onset_offset_metrics, num_pieces)

    # note metrics w/ onset predictions (precision/recall/f1-score
    mean_note_wise_onset_metrics_with_onset_pred = util.mean_eval_frame_wise(note_wise_onset_metrics_with_onset_pred,
                                                                             num_pieces)

    mean_note_wise_onset_offset_metrics_with_onset_pred = util.mean_eval_frame_wise(
        note_wise_onset_offset_metrics_with_onset_pred, num_pieces)

    # note metrics w/ onset prediction heuristic (precision/recall/f1-score
    mean_note_wise_onset_metrics_with_onset_pred_heuristic = util.mean_eval_frame_wise(note_wise_onset_metrics_with_onset_pred_heuristic,
                                                                             num_pieces)

    mean_note_wise_onset_offset_metrics_with_onset_pred_heuristic = util.mean_eval_frame_wise(
        note_wise_onset_offset_metrics_with_onset_pred_heuristic, num_pieces)

    # write all metrics to file
    file = open(save_dir + save_file, "w")
    file.write("frame-wise metrics (precision/recall/f1-score) \n")
    file.write("mean:                    " + str(mean_frame_wise) + "\n")
    file.write("mean (onset prediction): " + str(mean_frame_wise_with_onset_pred) + "\n")
    file.write("mean (onset heuristic):  " + str(mean_frame_wise_with_onset_pred_heuristic) + "\n")
    file.write("mean (onset only):       " + str(mean_frame_wise_onset) + "\n")
    for index in range(0, n_onset_plus):
        file.write("mean (onset + " + str(index+1) + " only):   " + str(mean_frame_wise_onset_plus[index]) + "\n")

    file.write("mean (offset only):      " + str(mean_frame_wise_offset) + "\n")

    file.write("\n")
    file.write("----------------------------------------------------------------- \n")
    file.write("\n")
    file.write("note metrics w/o onset predictions (precision/recall/f1-score) \n")

    file.write("mean (w/o offset): " + str(mean_note_wise_onset_metrics) + "\n")
    file.write("mean (w/ offset):  " + str(mean_note_wise_onset_offset_metrics) + "\n")

    file.write("\n")
    file.write("----------------------------------------------------------------- \n")
    file.write("\n")
    file.write("note metrics w/ onset predictions (precision/recall/f1-score) \n")

    file.write("mean (w/o offset): " + str(mean_note_wise_onset_metrics_with_onset_pred) + "\n")
    file.write("mean (w/ offset):  " + str(mean_note_wise_onset_offset_metrics_with_onset_pred) + "\n")

    file.write("\n")
    file.write("----------------------------------------------------------------- \n")
    file.write("\n")
    file.write("note metrics w/ onset predictions and heuristic (" + str(onset_duration_heuristic) + " frames) (precision/recall/f1-score) \n")

    file.write("mean (w/o offset): " + str(mean_note_wise_onset_metrics_with_onset_pred_heuristic) + "\n")
    file.write("mean (w/ offset):  " + str(mean_note_wise_onset_offset_metrics_with_onset_pred_heuristic) + "\n")

    file.close()


def transcribe_piano_piece(audio_file, net, model_dir, save_dir, onset_duration_heuristic, norm=False, use_rnn=False):
    """ps_inference.transcribe_piano_piece('C:/Users/cjaed/Documents/MAPS/ENSTDkAm/MUS/MAPS_MUS-chpn_op25_e4_ENSTDkAm.wav', 'ResNet_v1_RNN', './model/', './transcription/',0, False, True)"""
    config = ppp.get_preprocessing_parameters(0)
    audio_config = config['audio_config']

    # build predictor
    predictor, hparams = build_predictor(net, model_dir)
    # init madmom peak picker
    proc = madmom.features.notes.NotePeakPickingProcessor(threshold=0.1, fps=100)
    # init piano note processor for onset prediction
    rnn_processor = madmom.features.notes.RNNPianoNoteProcessor()
    print(audio_file.split('.')[0])
    spectrogram = prep.wav_to_spec("", audio_file.split('.')[0], audio_config)

    # re-scale spectrogram to the range [0, 1]
    if norm:
        spectrogram = np.divide(spectrogram, np.max(spectrogram))



    # get note activation fn from model
    if use_rnn:
        note_activation, onset_activation = spectrogram_to_non_overlap_note_activation(spectrogram, hparams['frames'], predictor)
    else:
        note_activation = spectrogram_to_note_activation(spectrogram, config['context_frames'], predictor)

    #frames = np.greater_equal(note_activation, 0.5)
    # smooth out activation fn
    #win = signal.hann(3)
    #for idx in range(0, 88):
    #    frames[:, idx] = signal.convolve(frames[:, idx], win, mode='same') / sum(win)

    frames = np.greater_equal(note_activation, 0.5)

    # get note onset processor
    rnn_act_fn = rnn_processor(audio_file)

    # predict onsets
    onset_predictions_timings = proc(rnn_act_fn)

    # transform onset predictions to piano roll representation
    # onset_predictions = util.piano_roll_rep(onset_frames=(onset_predictions_timings[:, 0] /
    #                                                       (1. / audio_config['fps'])).astype(int),
    #                                         midi_pitches=onset_predictions_timings[:, 1].astype(int) - 21,
    #                                         piano_roll_shape=np.shape(frames))
    #
    # onset_predictions_with_heuristic = util.piano_roll_rep(onset_frames=(onset_predictions_timings[:, 0] /
    #                                                                      (1. / audio_config['fps'])).astype(int),
    #                                                        midi_pitches=onset_predictions_timings[:, 1].astype(
    #                                                            int) - 21,
    #                                                        piano_roll_shape=np.shape(frames),
    #                                                        onset_duration=onset_duration_heuristic)

    onset_predictions = np.greater_equal(onset_activation, 0.5)
    onset_predictions_with_heuristic = np.greater_equal(onset_activation, 0.5)

    # add onset predictions and onset prediction with heuristic to transcription
    frames_with_onset = np.logical_or(frames, onset_predictions)
    frames_with_onset_heuristic = np.logical_or(frames, onset_predictions_with_heuristic)

    # transform the pianoroll to a interval sequence
    est_intervals, \
    est_pitches = util.pianoroll_to_interval_sequence(frames, frames_per_second=audio_config['fps'],
                                                      min_midi_pitch=21, onset_predictions=None,
                                                      offset_predictions=None)

    est_intervals_onset_pred, \
    est_pitches_onset_pred = util.pianoroll_to_interval_sequence(frames_with_onset,
                                                                 frames_per_second=audio_config['fps'],
                                                                 min_midi_pitch=21,
                                                                 onset_predictions=onset_predictions,
                                                                 offset_predictions=None)

    est_intervals_onset_pred_heuristic, \
    est_pitches_onset_pred_heuristic = util.pianoroll_to_interval_sequence(
        frames_with_onset_heuristic, frames_per_second=
        audio_config['fps'],
        min_midi_pitch=21,
        onset_predictions=onset_predictions,
        offset_predictions=None)

    # convert intervals and pitches to ‘onset time’ ‘note number’ [‘duration’ [‘velocity’ [‘channel’]]] for digestion by madmom
    notes = np.stack((est_intervals[:, 0], est_pitches, est_intervals[:, 1]-est_intervals[:, 0]), axis=1)
    notes_onset_pred = np.stack((est_intervals_onset_pred[:, 0], est_pitches_onset_pred, est_intervals_onset_pred[:, 1] - est_intervals_onset_pred[:, 0]), axis=1)
    notes_onset_pred_heuristic = np.stack((est_intervals_onset_pred_heuristic[:, 0], est_pitches_onset_pred_heuristic, est_intervals_onset_pred_heuristic[:, 1] - est_intervals_onset_pred_heuristic[:, 0]), axis=1)

    # save midi files
    madmom.io.midi.write_midi(notes, save_dir + (audio_file.split('/')[-1]).split('.')[0] + "_noOnset.mid", duration=0.6, velocity=100)
    madmom.io.midi.write_midi(notes_onset_pred, save_dir + (audio_file.split('/')[-1]).split('.')[0] + "_onsetPrediction.mid", duration=0.6, velocity=100)
    madmom.io.midi.write_midi(notes_onset_pred_heuristic, save_dir + (audio_file.split('/')[-1]).split('.')[0] + "_onsetHeuristic.mid", duration=0.6, velocity=100)

    savemat(save_dir + (audio_file.split('/')[-1]).split('.')[0], {"note_activation": note_activation, "spec": spectrogram, "onset_act_fn": rnn_act_fn})


def get_serving_input_fn(frames, bins):
    def serving_input_fn():
        x = tf.placeholder(dtype=tf.float32, shape=[frames, bins], name='features')
        return tf.estimator.export.TensorServingInputReceiver(x, x)

    return serving_input_fn


def build_predictor(net, model_dir):
    hparams = php.get_hyper_parameters(net)
    classifier = tf.estimator.Estimator(
        model_fn=ps_model.conv_net_model_fn,
        model_dir=model_dir,
        # warm_start_from=model_dir,
        params=hparams)

    estimator_predictor = tf.contrib.predictor.from_estimator(classifier,
                                                              get_serving_input_fn(hparams['frames'],
                                                                                   hparams['freq_bins']),
                                                              output_key='predictions')
    return estimator_predictor, hparams


def get_activation(features, estimator_predictor):
    p = estimator_predictor({'input': features})

    return p['probabilities'], p['onset_probabilities']


def spectrogram_to_note_activation(spec, context_frames, estimator_predictor):
    note_activation = np.zeros([spec.shape[0], 88])
    for frame in range(context_frames, spec.shape[0] - context_frames):
        note_activation[frame, :] = get_activation(spec[frame - context_frames:frame + context_frames + 1, :],
                                                   estimator_predictor)
    return np.append(note_activation[8:], np.zeros([8, 88]), axis=0)
    #return note_activation


def spectrogram_to_non_overlap_note_activation(spec, context_frames, estimator_predictor):
    note_activation = np.zeros([1, 88])
    onset_activation = np.zeros([1, 88])
    split_spec = list(util.chunks(spec, context_frames))
    pad_length = context_frames - split_spec[-1].shape[0]
    #print(pad_length)
    split_spec[-1] = np.append(split_spec[-1], np.zeros([pad_length, split_spec[-1].shape[1]]), axis=0)
    #print(split_spec[-1].shape)
    for split in split_spec:
        frame_act, onset_act = get_activation(split, estimator_predictor)
        frame_act = np.squeeze(frame_act)
        onset_act = np.squeeze(onset_act)
        #print(act_fn.shape)
        note_activation = np.append(note_activation, frame_act, axis=0)
        onset_activation = np.append(onset_activation, onset_act, axis=0)

    #return note_activation[1:spec.shape[0]+1]
    return note_activation[3:spec.shape[0]+3], onset_activation[1:spec.shape[0]+1]
