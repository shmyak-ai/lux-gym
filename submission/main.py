import os
import time
import pickle
import numpy as np
import tensorflow as tf
import tensorflow.keras as keras

import tools
from action_vectors import meaning_vector, actions_number
from game import Game


class ResidualUnit(keras.layers.Layer):
    def __init__(self, filters, initializer, activation, **kwargs):
        super().__init__(**kwargs)

        self._filters = filters
        self._activation = activation
        self._conv = keras.layers.Conv2D(filters, 3, kernel_initializer=initializer, padding="same", use_bias=False)
        self._norm = keras.layers.BatchNormalization()

    def call(self, inputs, training=False, **kwargs):
        x = self._conv(inputs)
        x = self._norm(x, training=training)
        return self._activation(inputs + x)

    def compute_output_shape(self, batch_input_shape):
        batch, x, y, _ = batch_input_shape
        return [batch, x, y, self._filters]


class ResidualModel(keras.Model):
    def __init__(self, actions_n, **kwargs):
        super().__init__(**kwargs)

        filters = 128
        layers = 12

        initializer = keras.initializers.VarianceScaling(scale=2.0, mode='fan_in', distribution='truncated_normal')
        initializer_random = keras.initializers.random_uniform(minval=-0.03, maxval=0.03)
        activation = keras.activations.relu

        self._conv = keras.layers.Conv2D(filters, 3, padding="same", kernel_initializer=initializer, use_bias=False)
        self._norm = keras.layers.BatchNormalization()
        self._activation = keras.layers.ReLU()
        self._residual_block = [ResidualUnit(filters, initializer, activation) for _ in range(layers)]

        self._depthwise = keras.layers.DepthwiseConv2D(32)
        self._flatten = keras.layers.Flatten()

        # self._city_tiles_probs0 = keras.layers.Dense(128, activation=activation, kernel_initializer=initializer)
        # self._city_tiles_probs1 = keras.layers.Dense(4, activation="softmax",
        #                                              kernel_initializer=initializer_random)
        self._workers_probs0 = keras.layers.Dense(128, activation=activation, kernel_initializer=initializer)
        self._workers_probs1 = keras.layers.Dense(actions_n, activation="softmax",
                                                  kernel_initializer=initializer_random)
        # self._carts_probs0 = keras.layers.Dense(128, activation=activation, kernel_initializer=initializer)
        # self._carts_probs1 = keras.layers.Dense(17, activation="softmax", kernel_initializer=initializer_random)

        self._baseline = keras.layers.Dense(1, kernel_initializer=initializer_random,
                                            activation=keras.activations.tanh)

    def call(self, inputs, training=False, mask=None):
        features = inputs

        x = features

        x = self._conv(x)
        x = self._norm(x, training=training)
        x = self._activation(x)

        for layer in self._residual_block:
            x = layer(x, training=training)

        shape_x = tf.shape(x)
        y = tf.reshape(x, (shape_x[0], -1, shape_x[-1]))
        y = tf.reduce_mean(y, axis=1)

        z1 = (x * features[:, :, :, :1])
        shape_z = tf.shape(z1)
        z1 = tf.reshape(z1, (shape_z[0], -1, shape_z[-1]))
        z1 = tf.reduce_sum(z1, axis=1)
        z2 = self._depthwise(x)
        z2 = self._flatten(z2)
        z = tf.concat([z1, z2], axis=1)

        # t = self._city_tiles_probs0(z)
        # t = self._city_tiles_probs1(t)
        w = self._workers_probs0(z)
        w = self._workers_probs1(w)
        # c = self._carts_probs0(z)
        # c = self._carts_probs1(c)
        # probs = tf.concat([t, w, c], axis=1)
        # probs = probs * actions_mask
        probs = w

        baseline = self._baseline(tf.concat([y, z], axis=1))

        return probs, baseline

    def get_config(self):
        pass


def get_policy():
    feature_maps_shape = (32, 32, 59)
    model = ResidualModel(actions_number)
    dummy_input = tf.ones(feature_maps_shape, dtype=tf.float32)
    dummy_input = tf.nest.map_structure(lambda x: tf.expand_dims(x, axis=0), dummy_input)
    model(dummy_input)
    path = '/kaggle_simulations/agent' if os.path.exists('/kaggle_simulations') else '.'
    with open(f'{path}/data.pickle', 'rb') as file:
        init_data = pickle.load(file)
    model.set_weights(init_data['weights'])

    @tf.function(experimental_relax_shapes=True)
    def predict(obs):
        return model(obs)

    def policy(current_game_state, observation):
        actions = []
        workers_actions_probs_dict = {}
        workers_actions_dict = {}
        citytiles_actions_probs_dict = {}
        citytiles_actions_dict = {}
        actions_probs_dict = {"workers": workers_actions_probs_dict,
                              "carts": {},
                              "city_tiles": citytiles_actions_probs_dict}
        actions_dict = {"workers": workers_actions_dict,
                        "carts": {},
                        "city_tiles": citytiles_actions_dict}

        print(f"Step: {observation['step']}; Player: {observation['player']}")
        t1 = time.perf_counter()
        proc_observations = tools.get_separate_outputs(observation, current_game_state)
        t2 = time.perf_counter()
        print(f"1. Observations processing: {t2 - t1:0.4f} seconds")

        player = current_game_state.players[observation.player]

        unit_count = len(player.units)
        for city in player.cities.values():
            for city_tile in city.citytiles:
                if city_tile.can_act():
                    if unit_count < player.city_tile_count:
                        actions.append(city_tile.build_worker())
                        unit_count += 1
                    elif not player.researched_uranium():
                        actions.append(city_tile.research())
                        player.research_points += 1

        # workers
        if proc_observations["workers"]:
            t1 = time.perf_counter()
            workers_obs = np.stack(list(proc_observations["workers"].values()), axis=0)
            workers_obs = tf.nest.map_structure(lambda z: tf.cast(z, dtype=tf.float32), workers_obs)
            acts, vals = predict(workers_obs)
            # acts = tf.nn.softmax(tf.math.log(acts) * 2)  # sharpen distribution
            t2 = time.perf_counter()
            print(f"2. Workers prediction: {t2 - t1:0.4f} seconds")
            for i, key in enumerate(proc_observations["workers"].keys()):
                workers_actions_probs_dict[key] = acts[i, :].numpy()
                max_arg = tf.squeeze(tf.random.categorical(tf.math.log(acts[i:i+1]), 1))
                action_one_hot = tf.one_hot(max_arg, actions_number)
                workers_actions_dict[key] = action_one_hot.numpy()
                # deserialization
                meaning = meaning_vector[max_arg.numpy()]
                if meaning[0] == "m":
                    action_string = f"{meaning[0]} {key} {meaning[1]}"
                elif meaning[0] == "p":
                    action_string = f"{meaning[0]} {key}"
                elif meaning[0] == "t":
                    action_string = f"m {key} c"  # move center instead
                elif meaning[0] == "bcity":
                    action_string = f"{meaning[0]} {key}"
                else:
                    raise ValueError
                actions.append(action_string)

        return actions, actions_dict, actions_probs_dict, proc_observations
    return policy


game_state = None
current_policy = get_policy()


def agent(observation, configuration):
    global game_state

    if observation["step"] == 0:
        game_state = Game()
        game_state._initialize(observation["updates"])
        game_state._update(observation["updates"][2:])
        game_state.id = observation.player
    else:
        game_state._update(observation["updates"])

    actions, _, _, _ = current_policy(game_state, observation)
    return actions