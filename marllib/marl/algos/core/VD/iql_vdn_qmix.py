# MIT License

# Copyright (c) 2023 Replicable-MARL

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import tree  # pip install dm_tree
import torch
import torch.nn as nn
from gym.spaces import Dict as Gym_Dict
from ray.rllib.policy.torch_policy import TorchPolicy
from ray.rllib.policy.policy import Policy
from ray.rllib.models.modelv2 import _unpack_obs
from ray.rllib.agents.qmix.model import RNNModel, _get_size
from ray.rllib.execution.replay_buffer import *
from ray.rllib.models.torch.torch_action_dist import TorchCategorical
from ray.rllib.utils.metrics.learner_info import LEARNER_STATS_KEY
from ray.rllib.models.catalog import ModelCatalog
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.agents.qmix.qmix_policy import _mac, _validate, _unroll_mac
from ray.rllib.agents.dqn.dqn import GenericOffPolicyTrainer
from ray.rllib.agents.qmix.qmix import DEFAULT_CONFIG
from ray.rllib.policy.rnn_sequencing import chop_into_sequences
from ray.rllib.agents.dqn.simple_q_torch_policy import SimpleQTorchPolicy

from marllib.marl.models.zoo.rnn.jointQ_rnn import JointQRNN
from marllib.marl.models.zoo.mlp.jointQ_mlp import JointQMLP

from marllib.marl.models.zoo.mixer import QMixer, VDNMixer
from marllib.marl.algos.utils.episode_execution_plan import episode_execution_plan
from marllib.marl.algos.manager_utils.manager import Manager
from marllib.marl.algos.manager_utils.model_saver import ModelSaver

#### MODDED IMPORTS #####
from ray.rllib.agents.trainer import with_common_config
from ray.rllib.utils.typing import TrainerConfigDict


###### MODS FOR IQL ########
IQL_CONFIG = with_common_config({
    # === QMix ===
    # Mixing network. Either "qmix", "vdn", or None
    "mixer": None,
    # Size of the mixing network embedding
    "mixing_embed_dim": 32,
    # Whether to use Double_Q learning
    "double_q": True,
    # Optimize over complete episodes by default.
    "batch_mode": "complete_episodes",

    # === Exploration Settings ===
    "exploration_config": {
        # The Exploration class to use.
        "type": "EpsilonGreedy",
        # Config for the Exploration class' constructor:
        "initial_epsilon": 1.0,
        "final_epsilon": 0.02,
        "epsilon_timesteps": 10000,  # Timesteps over which to anneal epsilon.

        # For soft_q, use:
        # "exploration_config" = {
        #   "type": "SoftQ"
        #   "temperature": [float, e.g. 1.0]
        # }
    },

    # === Evaluation ===
    # Evaluate with epsilon=0 every `evaluation_interval` training iterations.
    # The evaluation stats will be reported under the "evaluation" metric key.
    # Note that evaluation is currently not parallelized, and that for Ape-X
    # metrics are already only reported for the lowest epsilon workers.
    "evaluation_interval": None,
    # Number of episodes to run per evaluation period.
    "evaluation_num_episodes": 10,
    # Switch to greedy actions in evaluation workers.
    "evaluation_config": {
        "explore": False,
    },

    # Number of env steps to optimize for before returning
    "timesteps_per_iteration": 1000,
    # Update the target network every `target_network_update_freq` steps.
    "target_network_update_freq": 500,

    # === Replay buffer ===
    # Size of the replay buffer in batches (not timesteps!).
    "buffer_size": 1000,

    # === Optimization ===
    # Learning rate for RMSProp optimizer
    "lr": 0.0005,
    # RMSProp alpha
    "optim_alpha": 0.99,
    # RMSProp epsilon
    "optim_eps": 0.00001,
    # If not None, clip gradients during optimization at this value
    "grad_norm_clipping": 10,
    # How many steps of the model to sample before learning starts.
    "learning_starts": 1000,
    # Update the replay buffer with this many samples at once. Note that
    # this setting applies per-worker if num_workers > 1.
    "rollout_fragment_length": 4,
    # Size of a batched sampled from replay buffer for training. Note that
    # if async_updates is set, then each worker returns gradients for a
    # batch of this size.
    "train_batch_size": 32,

    # === Parallelism ===
    # Number of workers for collecting samples with. This only makes sense
    # to increase if your environment is particularly slow to sample, or if
    # you"re using the Async or Ape-X optimizers.
    "num_workers": 0,
    # Whether to compute priorities on workers.
    "worker_side_prioritization": False,
    # Prevent iterations from going lower than this time span
    "min_iter_time_s": 1,

    # === Model ===
    "model": {
        "lstm_cell_size": 64,
        "max_seq_len": 999999,
    },
    # Only torch supported so far.
    "framework": "torch",
})

# original _unroll_mac for next observation is different from Pymarl.
# thus we provide a new JointQLoss here
class JointQLoss(nn.Module):
    def __init__(self,
                 model,
                 target_model,
                 mixer,
                 target_mixer,
                 n_agents,
                 n_actions,
                 double_q=True,
                 gamma=0.99):
        nn.Module.__init__(self)
        self.model = model
        self.target_model = target_model
        self.mixer = mixer
        self.target_mixer = target_mixer
        self.n_agents = n_agents
        self.n_actions = n_actions
        self.double_q = double_q
        self.gamma = gamma

    def forward(self,
                rewards,
                actions,
                terminated,
                mask,
                obs,
                next_obs,
                action_mask,
                next_action_mask,
                state=None,
                next_state=None):
        # import pdb; pdb.set_trace();
        """Forward pass of the loss.

        Args:
            rewards: Tensor of shape [B, T, n_agents]
            actions: Tensor of shape [B, T, n_agents]
            terminated: Tensor of shape [B, T, n_agents]
            mask: Tensor of shape [B, T, n_agents]
            obs: Tensor of shape [B, T, n_agents, obs_size]
            next_obs: Tensor of shape [B, T, n_agents, obs_size]
            action_mask: Tensor of shape [B, T, n_agents, n_actions]
            next_action_mask: Tensor of shape [B, T, n_agents, n_actions]
            state: Tensor of shape [B, T, state_dim] (optional)
            next_state: Tensor of shape [B, T, state_dim] (optional)
        """

        # Assert either none or both of state and next_state are given
        if state is None and next_state is None:
            state = obs  # default to state being all agents' observations
            next_state = next_obs
        elif (state is None) != (next_state is None):
            raise ValueError("Expected either neither or both of `state` and "
                             "`next_state` to be given. Got: "
                             "\n`state` = {}\n`next_state` = {}".format(
                state, next_state))

        # append the first element of obs + next_obs to get new one
        whole_obs = torch.cat((obs[:, 0:1], next_obs), axis=1)

        # Calculate estimated Q-Values
        mac_out = _unroll_mac(self.model, whole_obs)

        # Pick the Q-Values for the actions taken -> [B * n_agents, T]
        chosen_action_qvals = torch.gather(
            mac_out[:, :-1], dim=3, index=actions.unsqueeze(3)).squeeze(3)

        # Calculate the Q-Values necessary for the target

        target_mac_out = _unroll_mac(self.target_model, whole_obs)

        # we only need target_mac_out for raw next_obs part
        target_mac_out = target_mac_out[:, 1:]

        # Mask out unavailable actions for the t+1 step
        ignore_action_tp1 = (next_action_mask == 0) & (mask == 1).unsqueeze(-1)
        target_mac_out[ignore_action_tp1] = -np.inf

        # Max over target Q-Values
        if self.double_q:
            # large bugs here in original QMixloss, the gradient is calculated
            # we fix this follow pymarl
            mac_out_tp1 = mac_out.clone().detach()
            mac_out_tp1 = mac_out_tp1[:, 1:]

            # mask out unallowed actions
            mac_out_tp1[ignore_action_tp1] = -np.inf

            # obtain best actions at t+1 according to policy NN
            cur_max_actions = mac_out_tp1.argmax(dim=3, keepdim=True)

            # use the target network to estimate the Q-values of policy
            # network's selected actions
            target_max_qvals = torch.gather(target_mac_out, 3,
                                            cur_max_actions).squeeze(3)
        else:
            target_max_qvals = target_mac_out.max(dim=3)[0]

        assert target_max_qvals.min().item() != -np.inf, \
            "target_max_qvals contains a masked action; \
            there may be a state with no valid actions."

        # Mix
        if self.mixer is not None:
            chosen_action_qvals = self.mixer(chosen_action_qvals, state)
            target_max_qvals = self.target_mixer(target_max_qvals, next_state)

        # Calculate 1-step Q-Learning targets
        targets = rewards + self.gamma * (1 - terminated) * target_max_qvals

        # Td-error
        td_error = (chosen_action_qvals - targets.detach())

        mask = mask.expand_as(td_error)

        # 0-out the targets that came from padded data
        masked_td_error = td_error * mask

        # Normal L2 loss, take mean over actual data
        loss = (masked_td_error ** 2).sum() / mask.sum()
        return loss, mask, masked_td_error, chosen_action_qvals, targets


class JointQPolicy(Policy):
    do_cer = True

    def __init__(self, obs_space, action_space, config):
        _validate(obs_space, action_space)
        config = dict(ray.rllib.agents.qmix.qmix.DEFAULT_CONFIG, **config)
        self.framework = "torch"
        super().__init__(obs_space, action_space, config)
        self.n_agents = len(obs_space.original_space.spaces)
        config["model"]["n_agents"] = self.n_agents
        self.n_actions = action_space.spaces[0].n
        self.h_size = config["model"]["lstm_cell_size"]
        self.has_env_global_state = False
        self.has_action_mask = False
        self.device = (torch.device("cuda")
                       if torch.cuda.is_available() else torch.device("cpu"))
        self.reward_standardize = config["reward_standardize"]

        agent_obs_space = obs_space.original_space.spaces[0]
        if isinstance(agent_obs_space, Gym_Dict):
            space_keys = set(agent_obs_space.spaces.keys())
            if "obs" not in space_keys:
                raise ValueError(
                    "Dict obs space must have subspace labeled `obs`")
            self.obs_size = _get_size(agent_obs_space.spaces["obs"])
            if "action_mask" in space_keys:
                mask_shape = tuple(agent_obs_space.spaces["action_mask"].shape)
                if mask_shape != (self.n_actions,):
                    raise ValueError(
                        "Action mask shape must be {}, got {}".format(
                            (self.n_actions,), mask_shape))
                self.has_action_mask = True
            if "state" in space_keys:
                self.env_global_state_shape = _get_size(
                    agent_obs_space.spaces["state"])
                self.has_env_global_state = True
            else:
                self.env_global_state_shape = (self.obs_size, self.n_agents)
            # The real agent obs space is nested inside the dict
            config["model"]["full_obs_space"] = agent_obs_space
            agent_obs_space = agent_obs_space.spaces["obs"]
        else:
            self.obs_size = _get_size(agent_obs_space)
            self.env_global_state_shape = (self.obs_size, self.n_agents)

        core_arch = config["model"]["custom_model_config"]["model_arch_args"]["core_arch"]
        self.model = ModelCatalog.get_model_v2(
            agent_obs_space,
            action_space.spaces[0],
            self.n_actions,
            config["model"],
            framework="torch",
            name="model",
            default_model=JointQMLP if core_arch == "mlp" else JointQRNN).to(self.device)

        self.target_model = ModelCatalog.get_model_v2(
            agent_obs_space,
            action_space.spaces[0],
            self.n_actions,
            config["model"],
            framework="torch",
            name="target_model",
            default_model=JointQMLP if core_arch == "mlp" else JointQRNN).to(self.device)

        self.exploration = self._create_exploration()

        # Setup the mixer network.
        custom_config = config["model"]["custom_model_config"]
        if custom_config["global_state_flag"]:
            state_dim = custom_config["space_obs"]["state"].shape
        else:
            state_dim = custom_config["space_obs"]["obs"].shape + (custom_config["num_agents"],)
        if config["mixer"] is None:  # "iql"
            self.mixer = None
            self.target_mixer = None
        elif config["mixer"] == "qmix":
            self.mixer = QMixer(custom_config, state_dim).to(self.device)
            self.target_mixer = QMixer(custom_config, state_dim).to(self.device)
        elif config["mixer"] == "vdn":
            self.mixer = VDNMixer().to(self.device)
            self.target_mixer = VDNMixer().to(self.device)

        self.cur_epsilon = 1.0
        self.update_target()  # initial sync

        # Setup optimizer
        self.params = list(self.model.parameters())
        if self.mixer:
            self.params += list(self.mixer.parameters())
        self.loss = JointQLoss(self.model, self.target_model, self.mixer,
                               self.target_mixer, self.n_agents, self.n_actions,
                               self.config["double_q"], self.config["gamma"])

        if config["optimizer"] == "rmsprop":
            from torch.optim import RMSprop
            self.optimiser = RMSprop(
                params=self.params,
                lr=config["lr"])

        elif config["optimizer"] == "adam":
            from torch.optim import Adam
            self.optimiser = Adam(
                params=self.params,
                lr=config["lr"], )

        else:
            raise ValueError("choose one optimizer type from rmsprop(RMSprop) or adam(Adam)")

    @override(Policy)
    def compute_actions(self,
                        obs_batch,
                        state_batches=None,
                        prev_action_batch=None,
                        prev_reward_batch=None,
                        info_batch=None,
                        episodes=None,
                        explore=None,
                        timestep=None,
                        **kwargs):
        # print("hi\n\n\n")
        explore = explore if explore is not None else self.config["explore"]
        obs_batch, action_mask, _ = self._unpack_observation(obs_batch)
        # We need to ensure we do not use the env global state
        # to compute actions
        ModelSaver.dynamic_callback(self.model, _mac, state_batches, self.device)
        # Compute actions
        with torch.no_grad():
            q_values, hiddens = _mac(
                self.model,
                torch.as_tensor(
                    obs_batch, dtype=torch.float, device=self.device), [
                    torch.as_tensor(
                        np.array(s), dtype=torch.float, device=self.device)
                    for s in state_batches
                ])
            # print(q_values, obs_batch, [
            #         torch.as_tensor(
            #             np.array(s), dtype=torch.float, device=self.device).shape
            #         for s in state_batches
            #     ])
            
            avail = torch.as_tensor(
                action_mask, dtype=torch.float, device=self.device)
            masked_q_values = q_values.clone()
            masked_q_values[avail == 0.0] = -float("inf")
            masked_q_values_folded = torch.reshape(
                masked_q_values, [-1] + list(masked_q_values.shape)[2:])
            actions, _ = self.exploration.get_exploration_action(
                action_distribution=TorchCategorical(masked_q_values_folded),
                timestep=timestep,
                explore=explore)
            actions = torch.reshape(
                actions,
                list(masked_q_values.shape)[:-1]).cpu().numpy()
            hiddens = [s.cpu().numpy() for s in hiddens]

        return tuple(actions.transpose([1, 0])), hiddens, {}

    @override(Policy)
    def compute_log_likelihoods(self,
                                actions,
                                obs_batch,
                                state_batches=None,
                                prev_action_batch=None,
                                prev_reward_batch=None):
        obs_batch, action_mask, _ = self._unpack_observation(obs_batch)
        return np.zeros(obs_batch.size()[0])

    @override(Policy)
    def learn_on_batch(self, samples):
        obs_batch, action_mask, env_global_state = self._unpack_observation(
            samples[SampleBatch.CUR_OBS])
        (next_obs_batch, next_action_mask,
         next_env_global_state) = self._unpack_observation(
            samples[SampleBatch.NEXT_OBS])
        group_rewards = self._get_group_rewards(samples[SampleBatch.INFOS])

        input_list = [
            group_rewards, action_mask, next_action_mask,
            samples[SampleBatch.ACTIONS], samples[SampleBatch.DONES],
            obs_batch, next_obs_batch
        ]
        if self.has_env_global_state:
            input_list.extend([env_global_state, next_env_global_state])

        output_list, _, seq_lens = \
            chop_into_sequences(
                episode_ids=samples[SampleBatch.EPS_ID],
                unroll_ids=samples[SampleBatch.UNROLL_ID],
                agent_indices=samples[SampleBatch.AGENT_INDEX],
                feature_columns=input_list,
                state_columns=[],  # RNN states not used here
                max_seq_len=self.config["model"]["max_seq_len"],
                dynamic_max=True)
        # These will be padded to shape [B * T, ...]
        if self.has_env_global_state:
            (rew, action_mask, next_action_mask, act, dones, obs, next_obs,
             env_global_state, next_env_global_state) = output_list
        else:
            (rew, action_mask, next_action_mask, act, dones, obs,
             next_obs) = output_list
        B, T = len(seq_lens), max(seq_lens)

        def to_batches(arr, dtype):
            new_shape = [B, T] + list(arr.shape[1:])
            return torch.as_tensor(
                np.reshape(arr, new_shape), dtype=dtype, device=self.device)

        # reduce the scale of reward for small variance. This is also
        # because we copy the global reward to each agent in rllib_env
        rewards = to_batches(rew, torch.float) / self.n_agents
        if self.reward_standardize:
            rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-5)

        actions = to_batches(act, torch.long)
        obs = to_batches(obs, torch.float).reshape(
            [B, T, self.n_agents, self.obs_size])
        action_mask = to_batches(action_mask, torch.float)
        next_obs = to_batches(next_obs, torch.float).reshape(
            [B, T, self.n_agents, self.obs_size])
        next_action_mask = to_batches(next_action_mask, torch.float)
        if self.has_env_global_state:
            env_global_state = to_batches(env_global_state, torch.float)
            next_env_global_state = to_batches(next_env_global_state,
                                               torch.float)

        terminated = to_batches(dones, torch.float).unsqueeze(2).expand(
            B, T, self.n_agents)

        # Create mask for where index is < unpadded sequence length
        filled = np.reshape(
            np.tile(np.arange(T, dtype=np.float32), B),
            [B, T]) < np.expand_dims(seq_lens, 1)
        mask = torch.as_tensor(
            filled, dtype=torch.float, device=self.device).unsqueeze(2).expand(
            B, T, self.n_agents)

        # Compute loss
        loss_out, mask, masked_td_error, chosen_action_qvals, targets = (
            self.loss(rewards, actions, terminated, mask, obs, next_obs,
                      action_mask, next_action_mask, env_global_state,
                      next_env_global_state))

        # Optimise
        self.optimiser.zero_grad()
        loss_out.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(
            self.params, self.config["grad_norm_clipping"])
        self.optimiser.step()

        mask_elems = mask.sum().item()
        stats = {
            "loss": loss_out.item(),
            "grad_norm": grad_norm
            if isinstance(grad_norm, float) else grad_norm.item(),
            "td_error_abs": masked_td_error.abs().sum().item() / mask_elems,
            "q_taken_mean": (chosen_action_qvals * mask).sum().item() /
                            mask_elems,
            "target_mean": (targets * mask).sum().item() / mask_elems,
        }
        return {LEARNER_STATS_KEY: stats}

    @override(Policy)
    def get_initial_state(self):  # initial RNN state
        return [
            s.expand([self.n_agents, -1]).cpu().numpy()
            for s in self.model.get_initial_state()
        ]

    @override(Policy)
    def get_weights(self):
        return {
            "model": self._cpu_dict(self.model.state_dict()),
            "target_model": self._cpu_dict(self.target_model.state_dict()),
            "mixer": self._cpu_dict(self.mixer.state_dict())
            if self.mixer else None,
            "target_mixer": self._cpu_dict(self.target_mixer.state_dict())
            if self.mixer else None,
        }

    @override(Policy)
    def set_weights(self, weights):
        self.model.load_state_dict(self._device_dict(weights["model"]))
        self.target_model.load_state_dict(
            self._device_dict(weights["target_model"]))
        if weights["mixer"] is not None:
            self.mixer.load_state_dict(self._device_dict(weights["mixer"]))
            self.target_mixer.load_state_dict(
                self._device_dict(weights["target_mixer"]))

    @override(Policy)
    def get_state(self):
        state = self.get_weights()
        state["cur_epsilon"] = self.cur_epsilon
        return state

    @override(Policy)
    def set_state(self, state):
        self.set_weights(state)
        self.set_epsilon(state["cur_epsilon"])

    @override(Policy)
    def postprocess_trajectory(self,
                                sample_batch,
                                other_agent_batches=None,
                                episode=None):
        # Do all post-processing always with no_grad().
        # Not using this here will introduce a memory leak
        # in torch (issue #6962).
        with torch.no_grad():
            # # Call super's postprocess_trajectory first.
            sample_batch = super().postprocess_trajectory(
                sample_batch, other_agent_batches, episode)
            # import pdb;pdb.set_trace();
            # return sample_batch
            if self.do_cer:
                return Manager.dynamic_callback(self, sample_batch, other_agent_batches, episode)
            return sample_batch

                
    # @override(SimpleQTorchPolicy)
    # def extra_action_out(self, input_dict, state_batches, model,
    #                          action_dist):
    #     import pdb; pdb.set_trace()
    #     with self._no_grad_context():
    #         stats_dict = ModelSaver.dynamic_callback(self, input_dict, state_batches, model, action_dist)
    #     return self._convert_to_non_torch_type(stats_dict)

    def update_target(self):
        self.target_model.load_state_dict(self.model.state_dict())
        if self.mixer is not None:
            self.target_mixer.load_state_dict(self.mixer.state_dict())
        logger.debug("Updated target networks")

    def set_epsilon(self, epsilon):
        self.cur_epsilon = epsilon

    def _get_group_rewards(self, info_batch):
        group_rewards = np.array([
            info.get("_group_rewards", [0.0] * self.n_agents)
            for info in info_batch
        ])
        return group_rewards

    def _device_dict(self, state_dict):
        return {
            k: torch.as_tensor(v, device=self.device)
            for k, v in state_dict.items()
        }

    @staticmethod
    def _cpu_dict(state_dict):
        return {k: v.cpu().detach().numpy() for k, v in state_dict.items()}

    def _unpack_observation(self, obs_batch):
        """Unpacks the observation, action mask, and state (if present)
        from agent grouping.

        Returns:
            obs (np.ndarray): obs tensor of shape [B, n_agents, obs_size]
            mask (np.ndarray): action mask, if any
            state (np.ndarray or None): state tensor of shape [B, state_size]
                or None if it is not in the batch
        """

        unpacked = _unpack_obs(
            np.array(obs_batch, dtype=np.float32),
            self.observation_space.original_space,
            tensorlib=np)

        if isinstance(unpacked[0], dict):
            assert "obs" in unpacked[0]
            unpacked_obs = [
                np.concatenate(tree.flatten(u["obs"]), 1) for u in unpacked
            ]
        else:
            unpacked_obs = unpacked

        obs = np.concatenate(
            unpacked_obs,
            axis=1).reshape([len(obs_batch), self.n_agents, self.obs_size])

        if self.has_action_mask:
            action_mask = np.concatenate(
                [o["action_mask"] for o in unpacked], axis=1).reshape(
                [len(obs_batch), self.n_agents, self.n_actions])
        else:
            action_mask = np.ones(
                [len(obs_batch), self.n_agents, self.n_actions],
                dtype=np.float32)

        if self.has_env_global_state:
            state = np.concatenate(tree.flatten(unpacked[0]["state"]), 1)
        else:
            state = None
        return obs, action_mask, state
    
# def validate_config(config: TrainerConfigDict) -> None:
#     # Add the `burn_in` to the Model's max_seq_len.
#     # Set the replay sequence length to the max_seq_len of the model.
#         # config["replay_sequence_length"] = \
#         #     config["burn_in"] + config["model"]["max_seq_len"]

#         def f(batch, workers, config):
#             import pdb;pdb.set_trace();
#             return batch

#         config["before_learn_on_batch"] = f

JointQTrainer = GenericOffPolicyTrainer.with_updates(
    name="JointQ",
    default_config=IQL_CONFIG,
    default_policy=JointQPolicy,
    get_policy_class=None,
    execution_plan=episode_execution_plan, 
    validate_config=None,
    )

