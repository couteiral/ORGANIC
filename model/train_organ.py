from __future__ import absolute_import, division, print_function
# get free gpu
from gpu_utils import pick_gpu_lowest_memory
gpu_free_number = str(pick_gpu_lowest_memory())
# set enviroment variables
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '{}'.format(gpu_free_number)
from builtins import range
from collections import OrderedDict
import os
import model
import numpy as np
import tensorflow as tf
import random
import time
import json
from gen_dataloader import Gen_Data_loader
from dis_dataloader import Dis_dataloader
from text_classifier import TextCNN
from rollout import ROLLOUT
import pandas as pd
import importlib
import sys
import shutil
from tqdm import tqdm

if len(sys.argv) == 2:
    PARAM_FILE = sys.argv[1]
else:
    PARAM_FILE = 'exp.json'
params = json.loads(open(PARAM_FILE).read(), object_pairs_hook=OrderedDict)

##########################################################################
#  Training  Hyper-parameters
##########################################################################
mm = importlib.import_module(params['METRICS_FILE'])

PREFIX = params['EXP_NAME']
PRE_EPOCH_NUM = params['G_PRETRAIN_STEPS']
TRAIN_ITER = params['G_STEPS']  # generator
BATCH_SIZE = params["BATCH_SIZE"]
SEED = params['SEED']
dis_batch_size = 64
dis_num_epochs = 3
dis_alter_epoch = params['D_PRETRAIN_STEPS']

BATCHES = params['TOTAL_BATCH']
OBJECTIVE = params['OBJECTIVE']

if (type(BATCHES) is list) or (type(OBJECTIVE) is list):

    TRAINING_PROGRAM = True
    if type(OBJECTIVE) is not list or type(BATCHES) is not list:
        print("Unmatching training program parameters")
        raise
    if len(OBJECTIVE) != len(BATCHES):
        print("Unmatching training program parameters")
        raise
    TOTAL_BATCH = np.sum(np.asarray(BATCHES))

    i = 0
    education = {}
    for j, stage in enumerate(BATCHES):
        for _ in range(stage):
            education[i] = OBJECTIVE[j]
            i += 1
else:
    TRAINING_PROGRAM = False
    TOTAL_BATCH = BATCHES


##########################################################################

##########################################################################
#  Generator  Hyper-parameters
##########################################################################
EMB_DIM = 32
HIDDEN_DIM = 32
START_TOKEN = 0
SAMPLE_NUM = 6400
BIG_SAMPLE_NUM = SAMPLE_NUM * 5
D_WEIGHT = params['LAMBDA']

D = max(int(5 * D_WEIGHT), 1)
##########################################################################

##########################################################################
#  Discriminator  Hyper-parameters
##########################################################################
dis_embedding_dim = 64
dis_filter_sizes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20]
dis_num_filters = [100, 200, 200, 200, 200, 100, 100, 100, 100, 100, 160, 160]
dis_dropout_keep_prob = 0.75
dis_l2_reg_lambda = 0.2
##########################################################################

#============= Objective ==============


def make_reward(train_samples, nbatch):

    if TRAINING_PROGRAM == False:

        reward_func = mm.load_reward(OBJECTIVE)

        def batch_reward(samples):
            decoded = [mm.decode(sample, ord_dict) for sample in samples]
            pct_unique = len(list(set(decoded))) / float(len(decoded))
            rewards = reward_func(decoded, train_samples)
            weights = np.array([pct_unique / float(decoded.count(sample))
                                for sample in decoded])

            return rewards * weights

        return batch_reward

    else:

        reward_func = mm.load_reward(education[nbatch])

        def batch_reward(samples):
            decoded = [mm.decode(sample, ord_dict) for sample in samples]
            pct_unique = len(list(set(decoded))) / float(len(decoded))
            rewards = reward_func(decoded, train_samples)
            weights = np.array([pct_unique / float(decoded.count(sample))
                                for sample in decoded])

            return rewards * weights

        return batch_reward


def print_rewards(rewards):
    print('Rewards be like...')
    np.set_printoptions(precision=3, suppress=True)
    print(rewards)
    mean_r, std_r = np.mean(rewards), np.std(rewards)
    min_r, max_r = np.min(rewards), np.max(rewards)
    print('Mean: {:.3f} , Std:  {:.3f}'.format(mean_r, std_r), end='')
    print(', Min: {:.3f} , Max:  {:.3f}\n'.format(min_r, max_r))
    np.set_printoptions(precision=8, suppress=False)
    return
#=======================================
##########################################################################
train_samples = mm.load_train_data(params['TRAIN_FILE'])
char_dict, ord_dict = mm.build_vocab(train_samples)
NUM_EMB = len(char_dict)
DATA_LENGTH = max(map(len, train_samples))
MAX_LENGTH = params["MAX_LENGTH"]
to_use = [sample for sample in train_samples if mm.verified_and_below(
    sample, MAX_LENGTH)]
positive_samples = [mm.encode(sample, MAX_LENGTH, char_dict)
                    for sample in to_use]
POSITIVE_NUM = len(positive_samples)
print('Starting ObjectiveGAN for {:7s}'.format(PREFIX))
print('Data points in train_file {:7d}'.format(len(train_samples)))
print('Max data length is        {:7d}'.format(DATA_LENGTH))
print('Max length to use is      {:7d}'.format(MAX_LENGTH))
print('Avg length to use is      {:7f}'.format(
    np.mean([len(s) for s in to_use])))
print('Num valid data points is  {:7d}'.format(POSITIVE_NUM))
print('Size of alphabet is       {:7d}'.format(NUM_EMB))

mm.print_params(params)
##########################################################################


class Generator(model.LSTM):

    def g_optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(0.002)  # ignore learning rate


def generate_samples(sess, trainable_model, batch_size, generated_num, verbose=False):
    #  Generated Samples
    generated_samples = []
    start = time.time()
    for _ in range(int(generated_num / batch_size)):
        generated_samples.extend(trainable_model.generate(sess))
    end = time.time()
    if verbose:
        print('Sample generation time: %f' % (end - start))
    return generated_samples


def pre_train_epoch(sess, trainable_model, data_loader):
    supervised_g_losses = []
    data_loader.reset_pointer()

    for it in range(data_loader.num_batch):
        batch = data_loader.next_batch()
        _, g_loss, g_pred = trainable_model.pretrain_step(sess, batch)
        supervised_g_losses.append(g_loss)

    return np.mean(supervised_g_losses)


# This is a hack. I don't even use LIkelihood data loader tbh
likelihood_data_loader = Gen_Data_loader(BATCH_SIZE)


def pretrain(sess, generator, train_discriminator):
    # samples = generate_samples(sess, BATCH_SIZE, generated_num)
    gen_data_loader = Gen_Data_loader(BATCH_SIZE)
    gen_data_loader.create_batches(positive_samples)
    results = OrderedDict({'exp_name': PREFIX})

    #  pre-train generator
    print('Start pre-training...')
    start = time.time()
    for epoch in tqdm(range(PRE_EPOCH_NUM)):
        print(' gen pre-train')
        loss = pre_train_epoch(sess, generator, gen_data_loader)
        if epoch == 10 or epoch % 40 == 0:
            samples = generate_samples(sess, generator, BATCH_SIZE, SAMPLE_NUM)
            likelihood_data_loader.create_batches(samples)
            print('\t train_loss {}'.format(loss))
            mm.compute_results(samples, train_samples, ord_dict, results)

    samples = generate_samples(sess, generator, BATCH_SIZE, SAMPLE_NUM)
    likelihood_data_loader.create_batches(samples)

    samples = generate_samples(sess, generator, BATCH_SIZE, SAMPLE_NUM)
    likelihood_data_loader.create_batches(samples)

    print('Start training discriminator...')
    for i in tqdm(range(dis_alter_epoch)):
        print(' discriminator pre-train')
        d_loss, acc = train_discriminator()
    end = time.time()
    print('Total time was {:.4f}s'.format(end - start))
    return


def save_results(sess, folder, name, results_rows=None, nbatch=None):
    if results_rows is not None:
        df = pd.DataFrame(results_rows)
        df.to_csv('{}_results.csv'.format(folder), index=False)
    if nbatch is not None:
        label = 'final'
    else:
        label = str(nbatch)

    # save models
    model_saver = tf.train.Saver()
    ext_ckpt_dir = os.path.join(params['CHK_PATH'], folder)
    if not os.path.exists(ext_ckpt_dir):
        os.makedirs(ext_ckpt_dir)
    loc_ckpt_dir = os.path.join(os.getcwd(), folder)
    if not os.path.exists(loc_ckpt_dir):
        os.makedirs(loc_ckpt_dir)
    loc_ckpt_file = os.path.join(
        loc_ckpt_dir, '{}_{}.ckpt'.format(name, label))
    path = model_saver.save(sess, loc_ckpt_file)
    print('Model saved at {}'.format(path))
    shutil.copy(loc_ckpt_file, ext_ckpt_dir)
    print('Model copied to {}'.format(ext_ckpt_dir))
    return


def main():
    random.seed(SEED)
    np.random.seed(SEED)

    # assert START_TOKEN == 0

    vocab_size = NUM_EMB
    dis_data_loader = Dis_dataloader()

    best_score = 1000
    generator = Generator(vocab_size, BATCH_SIZE, EMB_DIM,
                          HIDDEN_DIM, MAX_LENGTH, START_TOKEN)

    with tf.variable_scope('discriminator'):
        cnn = TextCNN(
            sequence_length=MAX_LENGTH,
            num_classes=2,
            vocab_size=vocab_size,
            embedding_size=dis_embedding_dim,
            filter_sizes=dis_filter_sizes,
            num_filters=dis_num_filters,
            l2_reg_lambda=dis_l2_reg_lambda)

    cnn_params = [param for param in tf.trainable_variables()
                  if 'discriminator' in param.name]
    # Define Discriminator Training procedure
    dis_global_step = tf.Variable(0, name="global_step", trainable=False)
    dis_optimizer = tf.train.AdamOptimizer(1e-4)
    dis_grads_and_vars = dis_optimizer.compute_gradients(
        cnn.loss, cnn_params, aggregation_method=2)
    dis_train_op = dis_optimizer.apply_gradients(
        dis_grads_and_vars, global_step=dis_global_step)

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    sess = tf.Session(config=config)

    def train_discriminator():
        if D_WEIGHT == 0:
            return 0, 0

        negative_samples = generate_samples(
            sess, generator, BATCH_SIZE, POSITIVE_NUM)

        #  train discriminator
        dis_x_train, dis_y_train = dis_data_loader.load_train_data(
            positive_samples, negative_samples)
        dis_batches = dis_data_loader.batch_iter(
            zip(dis_x_train, dis_y_train), dis_batch_size, dis_num_epochs
        )

        for batch in dis_batches:
            x_batch, y_batch = zip(*batch)
            feed = {
                cnn.input_x: x_batch,
                cnn.input_y: y_batch,
                cnn.dropout_keep_prob: dis_dropout_keep_prob
            }
            _, step, loss, accuracy = sess.run(
                [dis_train_op, dis_global_step, cnn.loss, cnn.accuracy], feed)
        print('\tD loss  :   {}'.format(loss))
        print('\tAccuracy: {}'.format(accuracy))
        return loss, accuracy

    # Pretrain is checkpointed and only execcutes if we don't find a checkpoint
    saver = tf.train.Saver()
    ckpt_dir = 'checkpoints/{}_pretrain'.format(PREFIX)
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)
    ckpt_file = os.path.join(ckpt_dir, 'pretrain_ckpt')
    if os.path.isfile(ckpt_file + '.meta') and params["LOAD_PRETRAIN"]:
        saver.restore(sess, ckpt_file)
        print('Pretrain loaded from previous checkpoint {}'.format(ckpt_file))
    else:
        if params["LOAD_PRETRAIN"]:
            print('\t* No pre-training data found as {:s}.'.format(ckpt_file))
        else:
            print('\t* LOAD_PRETRAIN was set to false.')

        sess.run(tf.global_variables_initializer())
        pretrain(sess, generator, train_discriminator)
        path = saver.save(sess, ckpt_file)
        print('Pretrain finished and saved at {}'.format(path))

    rollout = ROLLOUT(generator, 0.8)

    print('#########################################################################')
    print('Start Reinforcement Training Generator...')
    results_rows = []
    for nbatch in tqdm(range(TOTAL_BATCH)):
        results = OrderedDict({'exp_name': PREFIX})
        batch_reward = make_reward(train_samples, nbatch)
        if nbatch % 1 == 0 or nbatch == TOTAL_BATCH - 1:
            print('* Making samples')
            if nbatch % 10 == 0:
                gen_samples = generate_samples(
                    sess, generator, BATCH_SIZE, BIG_SAMPLE_NUM)
            else:
                gen_samples = generate_samples(
                    sess, generator, BATCH_SIZE, SAMPLE_NUM)
            likelihood_data_loader.create_batches(gen_samples)
            print('batch_num: {}'.format(nbatch))
            results['Batch'] = nbatch

            # results
            mm.compute_results(gen_samples, train_samples, ord_dict, results)

        print('#########################################################################')
        print('-> Training generator with RL.')
        print('G Epoch {}'.format(nbatch))

        for it in range(TRAIN_ITER):
            samples = generator.generate(sess)
            rewards = rollout.get_reward(
                sess, samples, 16, cnn, batch_reward, D_WEIGHT)
            nll = generator.generator_step(sess, samples, rewards)
            # results
            print_rewards(rewards)
            print('neg-loglike: {}'.format(nll))
            results['neg-loglike'] = nll
        rollout.update_params()

        # generate for discriminator
        print('-> Training Discriminator')
        for i in range(D):
            print('D_Epoch {}'.format(i))
            d_loss, accuracy = train_discriminator()
            results['D_loss_{}'.format(i)] = d_loss
            results['Accuracy_{}'.format(i)] = accuracy
        print('results')
        results_rows.append(results)
        if nbatch % params["EPOCH_SAVES"] == 0:
            save_results(sess, PREFIX, PREFIX + '_model', results_rows, nbatch)

    # write results
    save_results(sess, PREFIX, PREFIX + '_model', results_rows)

    print('\n:*** FINISHED ***')
    return

if __name__ == '__main__':
    main()
