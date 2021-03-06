#!/usr/bin/python
#  -*- coding: utf-8 -*-
# author:  <yao62995@gmail.com> 

import numpy as np
from collections import deque

from common import *


class DDPG(Base):
    """
        Deep Deterministic Policy Gradient Model
        ref:
            paper "continuous control with deep reinforcement learning"
    """

    def __init__(self, states_dim, actions_dim, action_range=(-1, 1), train_dir="./ddpg_models", gpu_id=0,
                 observe=1e3, replay_memory=5e4, update_frequency=1, train_repeat=1, frame_seq_num=1, gamma=0.99,
                 batch_size=64, learn_rate=1e-3):
        Base.__init__(self)
        self.states_dim = states_dim
        self.actions_dim = actions_dim
        self.action_range = action_range
        self.gpu_id = gpu_id
        self.frame_seq_num = frame_seq_num
        # init train params
        self.observe = observe
        self.update_frequency = update_frequency
        self.train_repeat = train_repeat
        self.gamma = gamma
        # init replay memory deque
        self.replay_memory_size = replay_memory
        self.replay_memory = deque()
        # init noise
        self.explore_noise = OUNoise(self.actions_dim)
        # train models dir
        self.train_dir = train_dir
        if not os.path.isdir(self.train_dir):
            os.mkdir(self.train_dir)
        # init network params
        self.learn_rate = learn_rate
        self.batch_size = batch_size
        # tensorflow graph variables
        self.sess = None
        self.saver = None
        self.global_step = None
        self.ops = dict()
        # build graph
        self.build_graph()

    def target_exponential_moving_average(self, theta, tao=0.001):
        ema = tf.train.ExponentialMovingAverage(decay=1 - tao)
        update = ema.apply(var_list=theta)
        averages = [ema.average(x) for x in theta]
        return averages, update

    def get_variables(self, scope, shape, stddev=0.04, wd=0.01, collect="losses"):
        with tf.variable_scope(scope):
            weights = NetTools.variable_with_weight_decay('weights', shape=shape, stddev=stddev, wd=wd)
            biases = NetTools.get_variable('biases', [shape[-1]], tf.constant_initializer(0.1))
            weight_decay = tf.mul(tf.nn.l2_loss(weights), wd, name='weight_loss')
            tf.add_to_collection(collect, weight_decay)
            return weights, biases

    def actor_variables(self, scope, dim=512):
        with tf.variable_scope(scope):
            w1, b1 = self.get_variables("fc1", (self.states_dim * self.frame_seq_num, dim), wd=0.01, collect=scope)
            w2, b2 = self.get_variables("fc2", (dim, dim), wd=0.01, collect=scope)
            w3, b3 = self.get_variables("fc3", (dim, self.actions_dim), wd=0.01, collect=scope)
            return [w1, b1, w2, b2, w3, b3]

    def critic_variables(self, scope, dim=512):
        with tf.variable_scope(scope):
            w1, b1 = self.get_variables("fc1", (self.states_dim * self.frame_seq_num, dim), wd=0.01, collect=scope)
            w2, b2 = self.get_variables("fc2", (dim, dim), wd=0.01, collect=scope)
            w3, b3 = self.get_variables("fc3", (dim + self.actions_dim, dim), wd=0.01, collect=scope)
            w4, b4 = self.get_variables("fc4", (dim, 1), wd=0.01, collect=scope)
            return [w1, b1, w2, b2, w3, b3, w4, b4]

    def actor_network(self, op_scope, state, theta):
        weight = [theta[idx] for idx in xrange(0, len(theta), 2)]
        bias = [theta[idx] for idx in xrange(1, len(theta), 2)]
        with tf.variable_op_scope([state], op_scope, "actor") as scope:
            flat1 = tf.reshape(state, shape=(-1, self.states_dim * self.frame_seq_num), name="flat1")
            fc1 = tf.nn.relu(tf.matmul(flat1, weight[0]) + bias[0])
            fc2 = tf.nn.relu(tf.matmul(fc1, weight[1]) + bias[1])
            logits = tf.matmul(fc2, weight[2]) + bias[2]
            return logits

    def critic_network(self, op_scope, state, action, theta):
        weight = [theta[idx] for idx in xrange(0, len(theta), 2)]
        bias = [theta[idx] for idx in xrange(1, len(theta), 2)]
        with tf.variable_op_scope([state, action], op_scope, "critic") as scope:
            # reshape
            flat1 = tf.reshape(state, (-1, self.states_dim * self.frame_seq_num), name="flat1")
            fc1 = tf.nn.relu(tf.matmul(flat1, weight[0]) + bias[0])
            fc2 = tf.nn.relu(tf.matmul(fc1, weight[1]) + bias[1])
            h_concat = tf.concat(1, [fc2, action])
            fc3 = tf.nn.relu(tf.matmul(h_concat, weight[2]) + bias[2])
            fc4 = tf.matmul(fc3, weight[3]) + bias[3]
            logits = tf.squeeze(fc4, [1], name='out')
            return logits

    def build_graph(self, tao=0.001):
        with tf.Graph().as_default(), tf.device('/gpu:%d' % self.gpu_id):
            self.global_step = tf.get_variable('global_step', [],
                                               initializer=tf.constant_initializer(0), trainable=False)
            # init variables
            theta_p = self.actor_variables("actor")
            theta_q = self.critic_variables("critic")
            theta_pt, update_pt = self.target_exponential_moving_average(theta_p, tao=tao)
            theta_qt, update_qt = self.target_exponential_moving_average(theta_q, tao=tao)
            # actor network
            state = tf.placeholder(tf.float32, shape=(None, self.frame_seq_num, self.states_dim))
            act_logit = self.actor_network("actor", state, theta_p)
            cri_logit = self.critic_network("critic", state, act_logit, theta_q)
            # actor optimizer
            l2_loss = tf.add_n(tf.get_collection("actor"))
            p_loss = -tf.reduce_mean(cri_logit) + l2_loss
            opt_p = tf.train.AdamOptimizer(self.learn_rate)
            grad_var_theta_p = opt_p.compute_gradients(p_loss, var_list=theta_p)
            optimizer_p = opt_p.apply_gradients(grad_var_theta_p)
            with tf.control_dependencies([optimizer_p]):
                train_p = tf.group(update_pt)

            # train critic network
            q_target = tf.placeholder(tf.float32, shape=(None), name="critic_target")
            act_train = tf.placeholder(tf.float32, shape=(None, self.actions_dim), name="act_train")
            cri_train = self.critic_network("train_critic", state, act_train, theta_q)
            # target network
            state2 = tf.placeholder(tf.float32, shape=(None, self.frame_seq_num, self.states_dim))
            act_logit2 = self.actor_network("target_actor", state2, theta_pt)
            cri_logit2 = self.critic_network("target_critic", state2, act_logit2, theta_qt)
            # train critic optimizer
            l2_loss = tf.add_n(tf.get_collection("critic"))
            q_loss = tf.reduce_mean(tf.square(cri_train - q_target)) + l2_loss
            opt_q = tf.train.AdamOptimizer(self.learn_rate)
            grad_var_theta_q = opt_q.compute_gradients(q_loss, var_list=theta_q)
            optimizer_q = opt_q.apply_gradients(grad_var_theta_q, global_step=self.global_step)
            with tf.control_dependencies([optimizer_q]):
                train_q = tf.group(update_qt)

            # init session and saver
            self.saver = tf.train.Saver()
            self.sess = tf.Session(config=tf.ConfigProto(
                allow_soft_placement=True,
                log_device_placement=False)
            )
            self.sess.run(tf.initialize_all_variables())
        # restore model
        restore_model(self.sess, self.train_dir, self.saver)
        self.ops["act_logit"] = lambda obs: self.sess.run([act_logit], feed_dict={state: obs})
        # self.ops["act_noise"] = lambda obs: self.ops["act_logit"](obs) + self.explore_noise.noise()
        self.ops["cri_logit2"] = lambda obs: self.sess.run([cri_logit2], feed_dict={state2: obs})
        self.ops["train_p"] = lambda obs: self.sess.run([train_p, p_loss], feed_dict={state: obs})
        self.ops["train_q"] = lambda obs, act, q_t: self.sess.run([train_q, self.global_step, q_loss],
                                                                  feed_dict={state: obs, act_train: act, q_target: q_t})

    def get_action(self, state, with_noise=False):
        action = self.ops["act_logit"]([state])[0][0]
        if with_noise:
            action = np.clip(action + self.explore_noise.noise(), self.action_range[0], self.action_range[1])
        return action

    def feedback(self, state, action, reward, terminal, state_n):
        self.time_step += 1
        self.replay_memory.append((state, action, reward, terminal, state_n))
        if len(self.replay_memory) > self.replay_memory_size:
            self.replay_memory.popleft()
        if self.time_step > self.observe and self.time_step % self.update_frequency == 0:
            for _ in xrange(self.train_repeat):
                # train mini-batch from replay memory
                mini_batch = random.sample(self.replay_memory, self.batch_size)
                batch_state, batch_action = [], []
                batch_target_q = []
                for batch_i, sample in enumerate(mini_batch):
                    b_state, b_action, b_reward, b_terminal, b_state_n = sample
                    if b_terminal:
                        target_q = b_reward
                    else:  # compute target q values
                        target_q = b_reward + self.gamma * self.ops["cri_logit2"]([b_state_n])[0]
                    batch_state.append(b_state)
                    batch_action.append(b_action)
                    batch_target_q.append(target_q)
                # update actor network (theta_p)
                _, p_loss = self.ops["train_p"](batch_state)
                # update critic network (theta_q)
                _, global_step, q_loss = self.ops["train_q"](batch_state, batch_action, batch_target_q)
                if self.time_step % 1e3 == 0:
                    logger.info("step=%d, p_loss=%.6f, q_loss=%.6f" % (global_step, p_loss, q_loss))
        if self.time_step % 3e4 == 0:
            save_model(self.sess, self.train_dir, self.saver, "ddpg-", global_step=self.global_step)


if __name__ == "__main__":
    model = DDPG()
    # model.observe()
