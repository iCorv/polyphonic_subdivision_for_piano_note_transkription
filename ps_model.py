from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
from math import sqrt


def conv_net_model_fn(features, labels, mode, params):
    if params['data_format'] == 'NCHW' or params['data_format'] == 'channels_first':
        # Convert the inputs from channels_last (NHWC) to channels_first (NCHW).
        # This provides a large performance boost on GPU. See
        # https://www.tensorflow.org/performance/performance_guide#data_formats
        features = tf.reshape(features, [-1, params['num_channels'], params['frames'], params['freq_bins']])
        #features = tf.transpose(features, [0, 3, 1, 2])
    else:
        features = tf.reshape(features, [-1, params['frames'], params['freq_bins'], params['num_channels']])

    # learning_rate_fn = learning_rate_with_decay(
    #     initial_learning_rate=params['learning_rate'],
    #     batches_per_epoch=params['batches_per_epoch'],
    #     boundary_epochs=params['boundary_epochs'],
    #     decay_rates=params['decay_rates'])
    #
    # momentum_fn = momentum_with_decay(
    #     initial_momentum=params['momentum'],
    #     batches_per_epoch=params['batches_per_epoch'],
    #     boundary_epochs=params['boundary_epochs'],
    #     decay_rates=params['decay_rates_momentum'])

    learning_rate_fn = cycle_fn(params['learning_rate_cycle'], params['batches_per_epoch'], params['boundary_epochs'])

    momentum_fn = cycle_fn(params['momentum_cycle'], params['batches_per_epoch'], params['boundary_epochs'])

    # Empirical testing showed that including batch_normalization variables
    # in the calculation of regularized loss helped validation accuracy
    # for the CIFAR-10 dataset, perhaps because the regularization prevents
    # overfitting on the small data set. We therefore include all vars when
    # regularizing and computing loss during training.
    def loss_filter_fn(name):
        return 'conv' in name

    return conv_net_init(
        features=features,
        labels=labels,
        mode=mode,
        learning_rate_fn=learning_rate_fn,
        loss_filter_fn=loss_filter_fn,
        weight_decay=params['weight_decay'],
        momentum_fn=momentum_fn,
        clip_norm=params['clip_norm'],
        data_format=params['data_format'],
        batch_size=params['batch_size'],
        dtype=params['dtype'],
        num_classes=params['num_classes'],
        architecture=params['use_architecture'],
        use_rnn=params['use_rnn']
    )


def cycle_fn(
        cycle_factor, batches_per_epoch, boundary_epochs):
    """Get a learning rate that decays step-wise as training progresses.

    Args:
      initial_learning_rate: The start learning rate.
      batches_per_epoch: number of batches per epoch, sometimes called steps
      per epoch.
      boundary_epochs: list of ints representing the epochs at which we
        decay the learning rate.
      decay_rates: list of floats representing the decay rates to be used
        for scaling the learning rate. It should have one more element
        than `boundary_epochs`, and all elements should have the same type.

    Returns:
      Returns a function that takes a single argument - the number of batches
      trained so far (global_step)- and returns the learning rate to be used
      for training the next batch.
    """

    # Reduce the learning rate at certain epochs, for Example:
    boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
    vals = [learning_rate for learning_rate in cycle_factor]

    def cycle_rate_fn(global_step):
        global_step = tf.cast(global_step, tf.int32)
        return tf.train.piecewise_constant(global_step, boundaries, vals)

    return cycle_rate_fn


def learning_rate_with_decay(
        initial_learning_rate, batches_per_epoch, boundary_epochs, decay_rates):
    """Get a learning rate that decays step-wise as training progresses.

    Args:
      initial_learning_rate: The start learning rate.
      batches_per_epoch: number of batches per epoch, sometimes called steps
      per epoch.
      boundary_epochs: list of ints representing the epochs at which we
        decay the learning rate.
      decay_rates: list of floats representing the decay rates to be used
        for scaling the learning rate. It should have one more element
        than `boundary_epochs`, and all elements should have the same type.

    Returns:
      Returns a function that takes a single argument - the number of batches
      trained so far (global_step)- and returns the learning rate to be used
      for training the next batch.
    """

    # Reduce the learning rate at certain epochs, for Example:
    boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
    vals = [initial_learning_rate * decay for decay in decay_rates]

    def learning_rate_fn(global_step):
        global_step = tf.cast(global_step, tf.int32)
        return tf.train.piecewise_constant(global_step, boundaries, vals)

    return learning_rate_fn


def momentum_with_decay(
        initial_momentum, batches_per_epoch, boundary_epochs, decay_rates):
    """Get a learning rate that decays step-wise as training progresses.

    Args:
      initial_momentum: The start momentum.
      batches_per_epoch: number of batches per epoch, sometimes called steps
      per epoch.
      boundary_epochs: list of ints representing the epochs at which we
        decay the learning rate.
      decay_rates: list of floats representing the decay rates to be used
        for scaling the learning rate. It should have one more element
        than `boundary_epochs`, and all elements should have the same type.

    Returns:
      Returns a function that takes a single argument - the number of batches
      trained so far (global_step)- and returns the learning rate to be used
      for training the next batch.
    """

    # Reduce the learning rate at certain epochs, for Example:
    boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
    vals = [initial_momentum * decay for decay in decay_rates]

    def momentum_fn(global_step):
        global_step = tf.cast(global_step, tf.int32)
        return tf.train.piecewise_constant(global_step, boundaries, vals)

    return momentum_fn


def weights_from_labels(labels):
    labeled_examples = np.where(labels == 1.0)
    weights = np.zeros(np.shape(labels))
    weights[labeled_examples[0], :] = 1.0
    weights[labeled_examples] = 2.0
    return np.where(weights == 0.0, 0.25, weights)


def conv_net_init(features, labels, mode, learning_rate_fn, loss_filter_fn, weight_decay, momentum_fn, clip_norm, data_format, batch_size, architecture, use_rnn, dtype=tf.float32, num_classes=88):
    """Shared functionality for different model_fns.

    Initializes the ConvNet representing the model layers
    and uses that model to build the necessary EstimatorSpecs for
    the `mode` in question. For training, this means building losses,
    the optimizer, and the train op that get passed into the EstimatorSpec.
    For evaluation and prediction, the EstimatorSpec is returned without
    a train op, but with the necessary parameters for the given mode.

    Args:
      features: tensor representing input images
      labels: tensor representing class labels for all input images
      mode: current estimator mode; should be one of
        `tf.estimator.ModeKeys.TRAIN`, `EVALUATE`, `PREDICT`
      learning_rate_fn: returns the learning rate to be used
      for training the next batch.
      momentum: the momentum imposed by the optimizer.
      clip_norm: the value the logistic function of the output layer is clipped by.
      dtype: the TensorFlow dtype to use for calculations.

    Returns:
      EstimatorSpec parameterized according to the input params and the
      current mode.
    """

    # Generate a summary node for the images
    #tf.summary.image('images', features, max_outputs=6)

    features = tf.cast(features, dtype)
    #print(labels.shape)
    if mode != tf.estimator.ModeKeys.PREDICT:
        labels_unstacked = tf.unstack(labels, axis=1)
        print(labels_unstacked)
        label_frames = labels_unstacked[0]
        label_onset = labels_unstacked[1]

    if mode != tf.estimator.ModeKeys.PREDICT:
        labels = tf.cast(labels, dtype)


    if use_rnn:
        with tf.variable_scope('frames'):
            logits_1 = resnet_rnn(features, mode == tf.estimator.ModeKeys.TRAIN, batch_size=batch_size, data_format=data_format,
                                    num_classes=num_classes)
            probs_subdiv_1 = tf.sigmoid(logits_1)
            #probs_subdiv_1, _ = tf.split(probs_subdiv_1, num_or_size_splits=2, axis=2)
        with tf.variable_scope('onsets'):
            logits_2 = resnet_rnn(features, mode == tf.estimator.ModeKeys.TRAIN, batch_size=batch_size,
                                data_format=data_format,
                                num_classes=num_classes)
            probs_subdiv_2 = tf.sigmoid(logits_2)
            #_, probs_subdiv_2 = tf.split(probs_subdiv_2, num_or_size_splits=2, axis=2)
    else:
        logits = resnet(features, mode == tf.estimator.ModeKeys.TRAIN, data_format=data_format,
                            num_classes=num_classes)


    probs = []
    # tf.stop_gradient "pretends" to be a constant. In other words, inputs to this function are masked from the gradient computation.
    probs.append(tf.stop_gradient(probs_subdiv_1))
    probs.append(tf.stop_gradient(probs_subdiv_2))
    combined_probs = tf.concat(probs, axis=2)
    print(combined_probs.shape)

    # outputs = lstm_layer(
    #     combined_probs,
    #     batch_size=batch_size,
    #     num_units=256,
    #     lengths=None,
    #     # needs a vector of length batch size with the entries defining the length of each sequence. In case sequences differ in length
    #     stack_size=1,
    #     use_cudnn=True,
    #     is_training=mode == tf.estimator.ModeKeys.TRAIN,
    #     bidirectional=True)

    combined_probs = slim.fully_connected(combined_probs, 88, activation_fn=tf.sigmoid, scope='fc_frame')

    # Visualize conv1 kernels
    # with tf.variable_scope('conv1'):
    #     tf.get_variable_scope().reuse_variables()
    #     weights = tf.get_variable('weights')
    #     grid = put_kernels_on_grid(weights)
    #     tf.summary.image('conv1/kernels', grid, max_outputs=1)

    predictions = {
        'classes': tf.cast(tf.greater_equal(combined_probs, 0.5), tf.float32),
        'probabilities': combined_probs,
        'onset_probabilities': probs_subdiv_2,
        'logits': combined_probs
    }

    if mode == tf.estimator.ModeKeys.PREDICT:
        # Return the predictions and the specification for serving a SavedModel
        return tf.estimator.EstimatorSpec(
            mode=mode,
            predictions=predictions,
            export_outputs={'predictions': tf.estimator.export.PredictOutput(predictions)})

    #filler_tensor = tf.constant(0.0, shape=[batch_size, 2000, 44], dtype=dtype)
    #labels_1, labels_2 = tf.split(labels, num_or_size_splits=2, axis=2)
    #labels_1 = tf.concat([labels_1, filler_tensor], axis=2)
    #labels_2 = tf.concat([filler_tensor, labels_2], axis=2)

    #print("labels_1: " + str(labels_1.shape))

    if use_rnn:
        individual_loss_subdiv_1 = log_loss(label_frames, probs_subdiv_1, epsilon=clip_norm)
        individual_loss_subdiv_2 = log_loss(label_onset, probs_subdiv_2, epsilon=clip_norm)
        frame_losses = log_loss(combined_probs, label_frames, epsilon=clip_norm)
    else:
        individual_loss = log_loss(labels, tf.clip_by_value(predictions['probabilities'], clip_norm, 1.0 - clip_norm),
                                   epsilon=0.0)

    #loss = tf.reduce_mean(individual_loss_1)
    tf.losses.add_loss(tf.reduce_mean(individual_loss_subdiv_1))
    tf.losses.add_loss(tf.reduce_mean(individual_loss_subdiv_2))
    tf.losses.add_loss(tf.reduce_mean(frame_losses))
    loss = tf.losses.get_total_loss()


    if mode == tf.estimator.ModeKeys.TRAIN:
        global_step = tf.train.get_or_create_global_step()

        if use_rnn:
            learning_rate = tf.train.exponential_decay(
                0.0006,
                global_step,
                5000,
                0.98,
                staircase=True)
        else:
            learning_rate = learning_rate_fn(global_step)
            momentum = momentum_fn(global_step)

        # Create a tensor named learning_rate for logging purposes
        tf.identity(learning_rate, name='learning_rate')
        tf.summary.scalar('learning_rate', learning_rate)
        # Create a tensor named momentum for logging purposes
        #tf.identity(momentum, name='momentum')
        #tf.summary.scalar('momentum', momentum)

        if use_rnn:
            optimizer = tf.train.AdamOptimizer(learning_rate)
            train_op = slim.learning.create_train_op(
                loss,
                optimizer,
                clip_gradient_norm=3.0,
                summarize_gradients=True,
                variables_to_train=None)
        else:
            optimizer = tf.train.MomentumOptimizer(
                learning_rate=learning_rate,
                momentum=momentum,
                use_nesterov=True
            )
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                minimize_op = optimizer.minimize(loss, global_step)
            train_op = tf.group(minimize_op, update_ops)

    else:
        train_op = None

    fn = tf.metrics.false_negatives(label_frames, predictions['classes'])
    fp = tf.metrics.false_positives(label_frames, predictions['classes'])
    tp = tf.metrics.true_positives(label_frames, predictions['classes'])
    precision = tf.metrics.precision(label_frames, predictions['classes'])
    recall = tf.metrics.recall(label_frames, predictions['classes'])
    # this is the Kelz et al. def of frame wise metric F1
    f = tf.multiply(tf.constant(2.0), tf.multiply(precision[0], recall[0]))
    f = tf.divide(f, tf.add(precision[0], recall[0]))

    tf.identity(fn[0], name="fn")
    tf.identity(fp[0], name="fp")
    tf.identity(tp[0], name="tp")
    tf.identity(f, name="f1_score")

    # collect metrics
    metrics = {'false_negatives': fn,
               'false_positives': fp,
               'true_positives': tp,
               'precision': precision,
               'recall': recall,
               'f1_score': (f, tf.group(precision[1], recall[1]))}
               #'mean_iou': mean_iou}

    return tf.estimator.EstimatorSpec(
        mode=mode,
        predictions=predictions,
        loss=loss,
        train_op=train_op,
        eval_metric_ops=metrics)


_BATCH_NORM_DECAY = 0.997
_BATCH_NORM_EPSILON = 1e-5


def batch_norm(inputs, training, data_format):
    """Performs a batch normalization using a standard set of parameters."""
    # We set fused=True for a significant performance boost. See
    # https://www.tensorflow.org/performance/performance_guide#common_fused_ops
    return tf.layers.batch_normalization(
        inputs=inputs, axis=1 if data_format == 'channels_first' else 3,
        momentum=_BATCH_NORM_DECAY, epsilon=_BATCH_NORM_EPSILON, center=True,
        scale=True, training=training, fused=True)


def fixed_padding(inputs, kernel_size, data_format):
    """Pads the input along the spatial dimensions independently of input size.
    Args:
      inputs: A tensor of size [batch, channels, height_in, width_in] or
        [batch, height_in, width_in, channels] depending on data_format.
      kernel_size: The kernel to be used in the conv2d or max_pool2d operation.
                   Should be a positive integer.
      data_format: The input format ('channels_last' or 'channels_first').
    Returns:
      A tensor with the same format as the input with the data either intact
      (if kernel_size == 1) or padded (if kernel_size > 1).
    """
    pad_total = kernel_size - 1
    pad_beg = pad_total // 2
    pad_end = pad_total - pad_beg

    if data_format == 'channels_first':
        padded_inputs = tf.pad(inputs, [[0, 0], [0, 0],
                                        [pad_beg, pad_end], [pad_beg, pad_end]])
    else:
        padded_inputs = tf.pad(inputs, [[0, 0], [pad_beg, pad_end],
                                        [pad_beg, pad_end], [0, 0]])
    return padded_inputs


def conv2d_fixed_padding(inputs, filters, kernel_size, strides, padding, data_format):
    """Strided 2-D convolution with explicit padding."""
    # The padding is consistent and is based only on `kernel_size`, not on the
    # dimensions of `inputs` (as opposed to using `tf.layers.conv2d` alone).
    if strides > 1:
        inputs = fixed_padding(inputs, kernel_size, data_format)

    return tf.layers.conv2d(
        inputs=inputs, filters=filters, kernel_size=kernel_size, strides=strides,
        padding=padding, use_bias=False,
        kernel_initializer=tf.contrib.layers.variance_scaling_initializer(
              factor=2.0, mode='FAN_AVG', uniform=True),
        data_format=data_format)


def _building_block_v1(inputs, filters, training, projection_shortcut, strides, padding,
                       data_format):
    """A single block for ResNet v1, without a bottleneck.
    Convolution then batch normalization then ReLU as described by:
      Deep Residual Learning for Image Recognition
      https://arxiv.org/pdf/1512.03385.pdf
      by Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun, Dec 2015.
    Args:
      inputs: A tensor of size [batch, channels, height_in, width_in] or
        [batch, height_in, width_in, channels] depending on data_format.
      filters: The number of filters for the convolutions.
      training: A Boolean for whether the model is in training or inference
        mode. Needed for batch normalization.
      projection_shortcut: The function to use for projection shortcuts
        (typically a 1x1 convolution when downsampling the input).
      strides: The block's stride. If greater than 1, this block will ultimately
        downsample the input.
      data_format: The input format ('channels_last' or 'channels_first').
    Returns:
      The output tensor of the block; shape should match inputs.
    """
    shortcut = inputs

    if projection_shortcut is not None:
        shortcut = projection_shortcut(inputs)
        shortcut = batch_norm(inputs=shortcut, training=training,
                              data_format=data_format)

    inputs = conv2d_fixed_padding(
        inputs=inputs, filters=filters, kernel_size=3, strides=strides, padding=padding,
        data_format=data_format)
    inputs = batch_norm(inputs, training, data_format)
    inputs = tf.nn.relu(inputs)

    inputs = conv2d_fixed_padding(
        inputs=inputs, filters=filters, kernel_size=3, strides=1, padding='SAME',
        data_format=data_format)
    inputs = batch_norm(inputs, training, data_format)
    inputs += shortcut
    inputs = tf.nn.relu(inputs)

    return inputs


def resnet(inputs, is_training, data_format='channels_last', num_classes=88):
    """

    :param inputs:
    :param is_training:
    :param data_format:
    :param batch_size:
    :param num_classes:
    :return:
    """

    def projection_shortcut(inputs):
        return conv2d_fixed_padding(
            inputs=inputs, filters=64, kernel_size=1, strides=1, padding='SAME',
            data_format=data_format)

    def projection_shortcut_2(inputs):
        return conv2d_fixed_padding(
            inputs=inputs, filters=96, kernel_size=1, strides=1, padding='SAME',
            data_format=data_format)

    net = conv2d_fixed_padding(inputs=inputs, filters=32, kernel_size=3, strides=1, padding='SAME',
                               data_format=data_format)

    print(net.shape)

    net = _building_block_v1(inputs=net, filters=32, training=is_training, projection_shortcut=None,
                             strides=1, padding='SAME', data_format=data_format)

    print(net.shape)
    net = tf.layers.max_pooling2d(inputs=net, pool_size=[3, 1], strides=[2, 1], padding='VALID', # (HPCP, 15 frames) pool_size=[3, 1], strides=[2, 1], (spec, 5 frames) pool_size=[3, 2], strides=[1, 2]
                                  data_format=data_format)
    print(net.shape)
    net = tf.layers.dropout(net, 0.25, name='dropout2', training=is_training)

    net = _building_block_v1(inputs=net, filters=64, training=is_training, projection_shortcut=projection_shortcut, strides=1, padding='SAME',
                             data_format=data_format)

    print(net.shape)
    net = tf.layers.max_pooling2d(inputs=net, pool_size=[3, 2], strides=[2, 2], padding='VALID', # (HPCP, 15 frames) pool_size=[3, 2], strides=[2, 2], (spec, 5 frames) pool_size=[3, 2], strides=[1, 2]
                                  data_format=data_format)

    net = tf.layers.dropout(net, 0.25, name='dropout3', training=is_training)

    # Flatten
    print(net.shape)
    net = tf.layers.flatten(net)
    print(net.shape)

    net = tf.layers.dense(net, 512, activation=tf.nn.relu, kernel_initializer=tf.contrib.layers.variance_scaling_initializer(
              factor=2.0, mode='FAN_AVG', uniform=True))
    print(net.shape)
    net = tf.layers.dropout(net, 0.5, name='dropout2', training=is_training)
    net = tf.layers.dense(net, num_classes, activation=None, kernel_initializer=tf.contrib.layers.variance_scaling_initializer(
              factor=2.0, mode='FAN_AVG', uniform=True))
    print(net.shape)
    return net


def resnet_rnn(inputs, is_training, batch_size=8, data_format='channels_last', num_classes=88):
    """

    :param inputs:
    :param is_training:
    :param data_format:
    :param batch_size:
    :param num_classes:
    :return:
    """

    def projection_shortcut(skip_input):
        return conv2d_fixed_padding(
            inputs=skip_input, filters=96, kernel_size=1, strides=1, padding='SAME',
            data_format=data_format)

    net = conv2d_fixed_padding(inputs=inputs, filters=48, kernel_size=3, strides=1, padding='SAME',
                               data_format=data_format)

    print(net.shape)

    net = _building_block_v1(inputs=net, filters=48, training=is_training, projection_shortcut=None,
                             strides=1, padding='SAME', data_format=data_format)

    print(net.shape)
    net = tf.layers.max_pooling2d(inputs=net, pool_size=[1, 2], strides=[1, 2], padding='VALID', # comment for HPCP
                                  data_format=data_format)
    print(net.shape)
    net = tf.layers.dropout(net, 0.25, name='dropout1', training=is_training)

    net = _building_block_v1(inputs=net, filters=96, training=is_training, projection_shortcut=projection_shortcut, strides=1, padding='SAME',
                             data_format=data_format)

    print(net.shape)
    net = tf.layers.max_pooling2d(inputs=net, pool_size=[1, 2], strides=[1, 2], padding='VALID',
                                  data_format=data_format)

    net = tf.layers.dropout(net, 0.25, name='dropout2', training=is_training)

    #Flatten
    print(net.shape)
    dims = tf.shape(net)

    net = tf.reshape(
        net, (dims[0], dims[1], net.shape[2].value * net.shape[3].value),
        'flatten_end')
    #print(inputs.shape)
    #net = tf.squeeze(inputs, axis=1)
    print(net.shape)
    with slim.arg_scope(
            [slim.fully_connected],
            activation_fn=tf.nn.relu,
            weights_initializer=tf.contrib.layers.variance_scaling_initializer(
                factor=2.0, mode='FAN_AVG', uniform=True)):
        net = slim.fully_connected(net, 768, scope='fc1')
        print(net.shape)
        net = slim.dropout(net, 0.5, scope='dropout3', is_training=is_training)

    net = lstm_layer(
        net,
        batch_size=batch_size,
        num_units=256,
        lengths=None,
        # needs a vector of length batch size with the entries defining the length of each sequence. In case sequences differ in length
        stack_size=1,
        use_cudnn=True,
        is_training=is_training,
        bidirectional=True)
    # print(net.shape)
    # net = tf.slice(net, [0, 10, 0], [-1, 1, -1])
    print(net.shape)
    net = slim.fully_connected(net, num_classes, activation_fn=None, scope='fc3')
    print(net.shape)

    return net


def cudnn_lstm_layer(inputs,
                     batch_size,
                     num_units,
                     lengths=None,
                     stack_size=1,
                     rnn_dropout_drop_amt=0,
                     is_training=True,
                     bidirectional=True):
    """Create a LSTM layer that uses cudnn."""
    inputs_t = tf.transpose(inputs, [1, 0, 2])
    if lengths is not None:
        all_outputs = [inputs_t]
        for i in range(stack_size):
            with tf.variable_scope('stack_' + str(i)):
                with tf.variable_scope('forward'):
                    lstm_fw = tf.contrib.cudnn_rnn.CudnnLSTM(
                        num_layers=1,
                        num_units=num_units,
                        direction='unidirectional',
                        dropout=rnn_dropout_drop_amt,
                        kernel_initializer=tf.contrib.layers.variance_scaling_initializer(
                        ),
                        bias_initializer=tf.zeros_initializer(),
                    )

                c_fw = tf.zeros([1, batch_size, num_units], tf.float32)
                h_fw = tf.zeros([1, batch_size, num_units], tf.float32)

                outputs_fw, _ = lstm_fw(
                    all_outputs[-1], (h_fw, c_fw), training=is_training)

                combined_outputs = outputs_fw

                if bidirectional:
                    with tf.variable_scope('backward'):
                        lstm_bw = tf.contrib.cudnn_rnn.CudnnLSTM(
                            num_layers=1,
                            num_units=num_units,
                            direction='unidirectional',
                            dropout=rnn_dropout_drop_amt,
                            kernel_initializer=tf.contrib.layers
                                .variance_scaling_initializer(),
                            bias_initializer=tf.zeros_initializer(),
                        )

                    c_bw = tf.zeros([1, batch_size, num_units], tf.float32)
                    h_bw = tf.zeros([1, batch_size, num_units], tf.float32)

                    inputs_reversed = tf.reverse_sequence(
                        all_outputs[-1], lengths, seq_axis=0, batch_axis=1)
                    outputs_bw, _ = lstm_bw(
                        inputs_reversed, (h_bw, c_bw), training=is_training)

                    outputs_bw = tf.reverse_sequence(
                        outputs_bw, lengths, seq_axis=0, batch_axis=1)

                    combined_outputs = tf.concat([outputs_fw, outputs_bw], axis=2)

                all_outputs.append(combined_outputs)

        # for consistency with cudnn, here we just return the top of the stack,
        # although this can easily be altered to do other things, including be
        # more resnet like
        return tf.transpose(all_outputs[-1], [1, 0, 2])
    else:
        lstm = tf.contrib.cudnn_rnn.CudnnLSTM(
            num_layers=stack_size,
            num_units=num_units,
            direction='bidirectional' if bidirectional else 'unidirectional',
            dropout=rnn_dropout_drop_amt,
            kernel_initializer=tf.contrib.layers.variance_scaling_initializer(),
            bias_initializer=tf.zeros_initializer(),
        )
        stack_multiplier = 2 if bidirectional else 1
        c = tf.zeros([stack_multiplier * stack_size, batch_size, num_units],
                     tf.float32)
        h = tf.zeros([stack_multiplier * stack_size, batch_size, num_units],
                     tf.float32)
        outputs, _ = lstm(inputs_t, (h, c), training=is_training)
        outputs = tf.transpose(outputs, [1, 0, 2])

        return outputs


def lstm_layer(inputs,
               batch_size,
               num_units,
               lengths=None,
               stack_size=1,
               use_cudnn=False,
               rnn_dropout_drop_amt=0,
               is_training=True,
               bidirectional=True):
    """Create a LSTM layer using the specified backend."""
    if use_cudnn:
        return cudnn_lstm_layer(inputs, batch_size, num_units, lengths, stack_size,
                                rnn_dropout_drop_amt, is_training, bidirectional)
    else:
        assert rnn_dropout_drop_amt == 0
        cells_fw = [
            tf.contrib.cudnn_rnn.CudnnCompatibleLSTMCell(num_units)
            for _ in range(stack_size)
        ]
        cells_bw = [
            tf.contrib.cudnn_rnn.CudnnCompatibleLSTMCell(num_units)
            for _ in range(stack_size)
        ]
        with tf.variable_scope('cudnn_lstm'):
            (outputs, unused_state_f,
             unused_state_b) = tf.contrib.rnn.stack_bidirectional_dynamic_rnn(
                cells_fw,
                cells_bw,
                inputs,
                dtype=tf.float32,
                sequence_length=lengths,
                parallel_iterations=1)

        return outputs



def log_loss(labels, predictions, epsilon=1e-7, scope=None, weights=None):
    """Calculate log losses.
        Same as tf.losses.log_loss except that this returns the individual losses
        instead of passing them into compute_weighted_loss and returning their
        weighted mean. This is useful for eval jobs that report the mean loss. By
        returning individual losses, that mean loss can be the same regardless of
        batch size.
        Args:
            labels: The ground truth output tensor, same dimensions as 'predictions'.
            predictions: The predicted outputs.
            epsilon: A small increment to add to avoid taking a log of zero.
            scope: The scope for the operations performed in computing the loss.
            weights: Weights to apply to labels.
        Returns:
            A `Tensor` representing the loss values.
        Raises:
            ValueError: If the shape of `predictions` doesn't match that of `labels`.
    """
    with tf.name_scope(scope, "log_loss", (predictions, labels)) as scope:
        predictions = tf.to_float(predictions)
        labels = tf.to_float(labels)
        predictions.get_shape().assert_is_compatible_with(labels.get_shape())
        losses = -tf.multiply(labels, tf.log(predictions + epsilon)) - tf.multiply(
            (1 - labels), tf.log(1 - predictions + epsilon))
        if weights is not None:
            losses = tf.multiply(losses, weights)

        return losses


def l1_loss_fn(tensor, weight=1.0, scope=None):
    """Define a L1Loss, useful for regularize, i.e. lasso.
    Args:
      tensor: tensor to regularize.
      weight: scale the loss by this factor.
      scope: Optional scope for name_scope.
    Returns:
      the L1 loss op.
    """
    with tf.name_scope(scope, 'L1Loss', [tensor]):
        weight = tf.convert_to_tensor(weight,
                                      dtype=tensor.dtype.base_dtype,
                                      name='loss_weight')
        loss = tf.multiply(weight, tf.reduce_sum(tf.abs(tensor)), name='value')
        return loss


def put_kernels_on_grid(kernel, pad=1):

    """Visualize conv. layer filters or output as an image.
        Arranges filters into a grid, with some paddings between adjacent filters.
        Args:
            kernel: tensor of shape [Y, X, NumChannels, NumKernels]
            pad: number of black pixels around each filter (between them)
        Return:
            Tensor of shape [1, (Y+2*pad)*grid_Y, (X+2*pad)*grid_X, NumChannels].
    """
    # get shape of the grid. NumKernels == grid_Y * grid_X
    def factorization(n):
        for i in range(int(sqrt(float(n))), 0, -1):
            if n % i == 0:
                if i == 1: print('Who would enter a prime number of filters')
                return i, int(n / i)
    (grid_Y, grid_X) = factorization(kernel.get_shape()[3].value)
    #print ('grid: %d = (%d, %d)' % (kernel.get_shape()[3].value, grid_Y, grid_X))

    x_min = tf.reduce_min(kernel)
    x_max = tf.reduce_max(kernel)
    kernel = (kernel - x_min) / (x_max - x_min)

    # pad X and Y
    x = tf.pad(kernel, tf.constant([[pad, pad], [pad, pad], [0, 0], [0, 0]]), mode='CONSTANT')

    # X and Y dimensions, w.r.t. padding
    Y = kernel.get_shape()[0] + 2 * pad
    X = kernel.get_shape()[1] + 2 * pad

    channels = kernel.get_shape()[2]

    # put NumKernels to the 1st dimension
    x = tf.transpose(x, (3, 0, 1, 2))
    # organize grid on Y axis
    x = tf.reshape(x, tf.stack([grid_X, Y * grid_Y, X, channels]))

    # switch X and Y axes
    x = tf.transpose(x, (0, 2, 1, 3))
    # organize grid on X axis
    x = tf.reshape(x, tf.stack([1, X * grid_X, Y * grid_Y, channels]))

    # back to normal order (not combining with the next step for clarity)
    x = tf.transpose(x, (2, 1, 3, 0))

    # to tf.image_summary order [batch_size, height, width, channels],
    #   where in this case batch_size == 1
    x = tf.transpose(x, (3, 0, 1, 2))

    # scaling to [0, 255] is not necessary for tensorboard
    return x
