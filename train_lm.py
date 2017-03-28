#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
# import cv2
import tensorflow as tf
import os, argparse, importlib

from render import render_text, ascii_print

class FastSaver(tf.train.Saver):
    # HACK disable saving metagraphs
    def save(self, sess, save_path, global_step=None, latest_filename=None, meta_graph_suffix='meta', write_meta_graph=True, write_state=True):
        super(FastSaver, self).save(sess, save_path, global_step, latest_filename, meta_graph_suffix, False, write_state)

def get_optimizer(opt_name, learning_rate, momentum):
    if opt_name == 'adam':
        optimizer = tf.train.AdamOptimizer(learning_rate)
    elif opt_name == 'rmsprop':
        optimizer = tf.train.RMSPropOptimizer(learning_rate, momentum=momentum, centered=True)
    else:
        optimizer = tf.train.MomentumOptimizer(learning_rate, momentum=momentum)
    return optimizer

def conform(im, shape):
    h, w = shape
    ih, iw = im.shape
    if ih == h:
        return im
    if ih > h:
        # take the center crop
        y0 = (ih - h) / 2
        return im[y0:y0 + h]
    # shorter image, ih < h
    new_im = np.zeros(shape)
    y0 = (h - ih) / 2
    new_im[y0:y0 + ih] = im
    return new_im

def render_glyph(char, shape=(24, 24), font=None):
    return conform(render_text(char, font), shape)
    # return cv2.resize(render_text(char, font), shape)

def build_model(token_ids, seq_lens, vocab_size, embed_dim, rnn_dim):
    # encoder
    rnn_input = tf.contrib.layers.embed_sequence(token_ids, vocab_size, embed_dim)

    # rnn
    bs = tf.size(seq_lens)
    cell = tf.contrib.rnn.GRUBlockCell(rnn_dim)
    rnn_output, final_state = tf.nn.dynamic_rnn(cell, rnn_input, seq_lens, cell.zero_state(bs, 'float'))

    # decoder
    decoder_input = tf.reshape(rnn_output, (-1, rnn_dim))
    token_logit = tf.contrib.layers.fully_connected(
        inputs=decoder_input,
        num_outputs=vocab_size,
        biases_initializer=tf.zeros_initializer(),
        weights_initializer=tf.contrib.layers.xavier_initializer(),
        activation_fn=None,
        scope='token_logit',
    )

    seq_logits = tf.reshape(token_logit, (bs, -1, vocab_size))

    return seq_logits, final_state

def build_input_pipeline(corpus_path, vocabulary, batch_size, shuffle):
    filename_queue = tf.train.string_input_producer(tf.train.match_filenames_once(corpus_path), shuffle=True)
    reader = tf.TextLineReader()
    _, line = reader.read(filename_queue)
    seq_len = tf.shape(tf.string_split([line]).values)[0]

    if shuffle:
        batch = tf.train.shuffle_batch([line, seq_len], batch_size=batch_size, capacity=128 * batch_size, min_after_dequeue=50 * batch_size)
    else:
        batch = tf.train.batch([line, seq_len], batch_size=batch_size, capacity=128 * batch_size)

    tokens = tf.string_split(batch[0])
    ids = tf.sparse_tensor_to_dense(vocabulary.lookup(tokens), validate_indices=False)
    seq_lens = batch[1]

    return ids, seq_lens

def train(train_split_path, val_split_path, dict_path, log_dir, batch_size, vocab_size, n_oov_buckets, initial_lr, lr_decay_steps, lr_decay_rate, lr_staircase, no_grad_clip, clip_norm, opt_method, momentum, val_interval):
    # token to token-id lookup
    vocabulary = tf.contrib.lookup.string_to_index_table_from_file(dict_path, num_oov_buckets=n_oov_buckets, vocab_size=vocab_size)

    # input pipelines
    # train
    ids, seq_lens = build_input_pipeline(train_split_path, vocabulary, batch_size, shuffle=True)

    # validation
    val_ids, val_seq_lens = build_input_pipeline(val_split_path, vocabulary, batch_size, shuffle=False)

    # model
    embed_dim, rnn_dim = 100, 64
    with tf.variable_scope('model'):
        seq_logits, final_state = build_model(ids, seq_lens, vocab_size + n_oov_buckets, embed_dim, rnn_dim)

    # validation
    with tf.variable_scope('model', reuse=True):
        val_seq_logits, val_final_state = build_model(val_ids, val_seq_lens, vocab_size + n_oov_buckets, embed_dim, rnn_dim)

    # loss
    mask = tf.sequence_mask(seq_lens, dtype=tf.float32)
    loss = tf.contrib.seq2seq.sequence_loss(seq_logits, ids, mask, average_across_timesteps=True, average_across_batch=True)
    n_samples = tf.reduce_sum(mask)

    val_mask = tf.sequence_mask(val_seq_lens, dtype=tf.float32)
    val_loss = tf.contrib.seq2seq.sequence_loss(val_seq_logits, val_ids, val_mask, average_across_timesteps=True, average_across_batch=True)

    global_step = tf.contrib.framework.create_global_step()
    learning_rate = tf.train.exponential_decay(learning_rate=initial_lr, global_step=global_step, decay_steps=lr_decay_steps, decay_rate=lr_decay_rate, staircase=lr_staircase)
    optimizer = get_optimizer(opt_method, learning_rate, momentum)

    trainable_vars = tf.trainable_variables()
    # re-scale the loss to be the sum of losses of all time steps
    grads = tf.gradients(loss * n_samples, trainable_vars)
    grad_norm = tf.global_norm(grads)
    var_norm = tf.global_norm(trainable_vars)

    if no_grad_clip:
        normed_grads = grads
        clipped_norm = grad_norm
    else:
        # gradient clipping
        normed_grads, _ = tf.clip_by_global_norm(grads, clip_norm, grad_norm)
        clipped_norm = tf.minimum(clip_norm, grad_norm)

    update_op = optimizer.apply_gradients(zip(normed_grads, trainable_vars), global_step)

    # summaries
    tf.summary.scalar('train/loss', loss)
    tf.summary.scalar('train/perplexity', tf.exp(loss))
    tf.summary.scalar('train/learning_rate', learning_rate)
    tf.summary.scalar('model/gradient_norm', grad_norm)
    tf.summary.scalar('model/clipped_gradient_norm', clipped_norm)
    tf.summary.scalar('model/variable_norm', var_norm)
    for g, v in zip(normed_grads, trainable_vars):
        tf.summary.histogram(v.name, g)

    train_summary_op = tf.summary.merge_all()

    val_summary = tf.summary.merge([
        tf.summary.scalar('val/loss', val_loss),
        tf.summary.scalar('val/perplexity', tf.exp(val_loss)),
    ])

    summary_dir = os.path.join(log_dir, 'logs')
    checkpoint_dir = os.path.join(log_dir, 'checkpoints')
    writer = tf.summary.FileWriter(summary_dir, flush_secs=30)

    init_op = tf.global_variables_initializer()
    saver = FastSaver(keep_checkpoint_every_n_hours=1, max_to_keep=2)
    # save metagraph once
    saver.export_meta_graph(os.path.join(checkpoint_dir, 'model.meta'))

    sv = tf.train.Supervisor(
        is_chief=True,
        saver=saver,
        init_op=init_op,
        ready_op=tf.report_uninitialized_variables(tf.global_variables()),
        summary_writer=writer,
        summary_op=train_summary_op,
        global_step=global_step,
        logdir=checkpoint_dir,
        save_model_secs=30,
        save_summaries_secs=30,
        )

    config = tf.ConfigProto(gpu_options={'allow_growth': True})
    with sv.managed_session('', config=config) as sess, sess.as_default():
        gs = global_step.eval()
        if gs == 0:
            print '* starting training at global step', gs
        else:
            print '* resuming training at global step', gs

        sv.start_queue_runners(sess)
        while not sv.should_stop():
            sess.run(update_op)
            gs = global_step.eval()
            if gs % val_interval == 0:
                writer.add_summary(sess.run(val_summary), gs)

    sv.stop()

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-l', '--log-dir', required=True)
    parser.add_argument('-b', '--batch-size', type=int, default=16)
    parser.add_argument('-v', '--vocab-size', type=int, default=1000)
    parser.add_argument('-o', '--n-oov-buckets', type=int, default=1)
    parser.add_argument('--optimizer', choices=['adam', 'rmsprop', 'momentum'], default='adam')
    parser.add_argument('--momentum', type=float, default=0.)
    parser.add_argument('--initial-lr', type=float, default=1e-3)
    parser.add_argument('--lr-decay-rate', type=float, default=1.)
    parser.add_argument('--lr-decay-steps', type=int, default=10**5)
    parser.add_argument('--lr-staircase', action='store_true')
    parser.add_argument('--no-grad-clip', action='store_true', help='disable gradient clipping')
    parser.add_argument('--clip-norm', type=float, default=40.)
    parser.add_argument('-t', '--train-corpus', default='work/train.txt', help='path to the training split')
    parser.add_argument('-e', '--val-corpus', default='work/val.txt', help='path to the validation split')
    parser.add_argument('--dictionary', default='work/dict.txt', help='path to the dictionary file')
    parser.add_argument('--val-interval', type=int, default=16, help='interval of evaluation on the validation split')

    args = parser.parse_args()

    train(args.train_corpus, args.val_corpus, args.dictionary, args.log_dir, args.batch_size, args.vocab_size, args.n_oov_buckets, args.initial_lr, args.lr_decay_steps, args.lr_decay_rate, args.lr_staircase, args.no_grad_clip, args.clip_norm, args.optimizer, args.momentum, args.val_interval)
