import argparse
import random
import time

import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
from replay_buffer import ReplayBuffer

# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/dqn/#dqnpy
import itertools
import os
import random
import time
from dataclasses import dataclass

import gymnasium as gym
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import tyro
from replay_buffer import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter
from torch.distributions import Categorical

from torch.distributions.categorical import Categorical
from torch.utils.tensorboard import SummaryWriter
from ray import train
from ray.tune.search import Repeater
from ray.tune.search.hebo import HEBOSearch
import ray.tune as tune  # Import the missing package

def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer

config = {
  "exp_name": "QREPS",
  "seed": 1,
  "torch_deterministic": True,
  "cuda": True,
  "track": False,
  "wandb_project_name": "QREPS_CartPole-v1",
  "wandb_entity": 'TFG',
  "capture_video": False,
  "env_id": "LunarLander-v2",
  "total_timesteps": 500,
  "num_updates": 50,
  "buffer_size": 10000,
  "update_epochs": tune.choice([5, 50, 100, 300]),
  "update_policy_epochs": tune.choice([50, 300, 450]),
  "num_rollouts": tune.choice([2, 5, 8]),
  "num_envs": tune.choice([1, 4, 6]),
  "gamma": 0.99,
  "policy_lr_start": tune.choice([0.1, 2e-2, 2.5e-3]),
  "q_lr_start": tune.choice([0.1, 2e-2, 2.5e-3]),
  "q_lr_end":  0,
  "policy_lr_end":  0,
  "alpha":  tune.choice([0.2, 0.5, 2, 4, 6]),
  "eta": None,
  "beta": tune.choice([0.1, 0.01, 0.002, 4e-5]),
  "autotune":  tune.choice([True, False]),
  "target_entropy_scale": tune.choice([0.2, 0.35, 0.5, 0.89]),
  "use_linear_schedule":  tune.choice([True, False]),
  "saddle_point_optimization":  tune.choice([True, False]),
  "use_kl_loss": tune.choice([True, False]),
  "target_network_frequency": tune.choice([2, 4, 8, 16]),
  "tau": 1.0,
}


import logging
FORMAT = "[%(asctime)s]: %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)
SEED_OFFSET = 0


def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer

Q_HIST = []

def make_env(env_id, seed, idx, capture_video, run_name):
    def thunk():
        if capture_video and idx == 0:
            env = gym.make(env_id, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(env, f"videos/{run_name}")
        else:
            env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env.action_space.seed(seed)

        return env

    return thunk

def linear_schedule(start_e: float, end_e: float, duration: int, t: int):
    slope = (end_e - start_e) / duration
    return max(slope * t + start_e, end_e)

class ExponentiatedGradientSampler:
    def __init__(self, N, device, eta, beta=0.01):
        self.n = N
        self.eta = eta
        self.beta = beta

        self.h = torch.ones((self.n,)) / N
        self.z = torch.ones((self.n,)) / N

        self.prob_dist = Categorical(torch.ones((self.n,))/ N)
        self.device = device

    def reset(self):
        self.h = torch.ones((self.n,))
        self.z = torch.ones((self.n,))
        self.prob_dist = Categorical(torch.softmax(torch.ones((self.n,)), 0))
                                     
    def probs(self):
        return self.prob_dist.probs.to(self.device)
    
    def entropy(self):
        return self.prob_dist.entropy().to(self.device)
    
    def update(self, bellman):
        self.h = bellman -  self.eta * torch.log(self.n * self.probs())
        t = self.beta*self.h
        self.z = self.probs() * torch.exp(t)
        self.z = torch.clamp(self.z / (torch.sum(self.z)), min=1e-8, max=1.0)
        self.prob_dist = Categorical(self.z)

def empirical_bellman_error(observations, next_observations, actions, rewards, qnet, policy, gamma):
    qf, qf2, qf_target, qf2_target = qnet
    with torch.no_grad():
        v_target = torch.min(qf_target.get_values(next_observations, policy=policy)[1], qf2_target.get_values(next_observations, policy=policy)[1])
    # v_target = qf.get_values(next_observations, actions, policy)[1]
    q_features_1 = qf.get_values(observations, actions, policy)[0]
    q_features_2 = qf2.get_values(observations, actions, policy)[0]
    loss_1 = rewards + gamma * v_target - q_features_1
    loss_2 = rewards + gamma * v_target - q_features_2
    loss = loss_1 + loss_2
    Q_HIST.append(q_features_1.flatten().detach().numpy())
    return loss

def saddle(eta, observations, next_observations, actions, rewards, qnets, policy, gamma, sampler):
    qf, qf2, _, _ = qnets
    discount_term_1 = (1 - gamma) * qf.get_values(observations, actions, policy)[1].mean()
    discount_term_2 = (1 - gamma) * qf2.get_values(observations, actions, policy)[1].mean()
    discount_term = discount_term_1 + discount_term_2

    errors = torch.sum(sampler.probs().detach() * (empirical_bellman_error(observations, next_observations, actions, rewards, qnets, policy, gamma) - eta * torch.log(sampler.n * sampler.probs().detach()))) + discount_term
    return errors

def ELBE(eta, observations, next_observations, actions, rewards, qnets, policy, gamma, sampler=None):
    qf, qf2, _, _ = qnets
    discount_term_1 = (1 - gamma) * qf.get_values(observations, actions, policy)[1].mean()
    discount_term_2 = (1 - gamma) * qf2.get_values(observations, actions, policy)[1].mean()
    discount_term = discount_term_1 + discount_term_2
    
    errors = eta * torch.logsumexp(
        empirical_bellman_error(observations, next_observations, actions, rewards, qnets, policy, gamma) / eta, 0
    ) + discount_term
    return errors

def nll_loss(alpha, observations, next_observations, rewards, actions, log_likes, q_net, policy):
    qf, qf2, _, _ = q_net
    with torch.no_grad():
        min_q = torch.min(qf.get_values(observations, actions, policy)[0], qf2.get_values(observations, actions, policy)[0])

    weights = torch.clamp(min_q / alpha, -20, 20)
    _, log_likes, _, _ = policy.get_action(observations, actions)
    nll = -torch.mean(torch.exp(weights) * log_likes)
    return nll

def kl_loss(alpha, observations, next_observations, rewards, actions, log_likes, q_net, policy):
    qf, qf2, _, _ = q_net
    with torch.no_grad():
        min_q = torch.min(qf.get_values(observations, policy=policy)[0], qf2.get_values(observations, policy=policy)[0])
    _, _, newlogprob, probs = policy.get_action(observations, actions)
    actor_loss = torch.mean(probs * (alpha * (newlogprob-log_likes.detach()) - min_q.detach()))
    return actor_loss

def optimize_critic(eta, observations, next_observations, actions, rewards, q_net, policy, gamma, sampler, optimizer, steps=300, loss_fn=ELBE):
    def closure():
        optimizer.zero_grad()
        loss = loss_fn(eta, observations, next_observations, actions, rewards, q_net , policy, gamma, sampler)
        loss.backward()
        if sampler is not None: sampler.update(empirical_bellman_error(observations, next_observations, actions, rewards, q_net, policy, gamma))
        # nn.utils.clip_grad_norm_([param for group in optimizer.param_groups for param in group['params']], 1.0)
        return loss

    for i in range(steps):
        optimizer.step(closure)

def optimize_actor(alpha, observations, next_observations, rewards, actions, log_likes, q_net, policy, optimizer, steps=300, loss_fn=nll_loss):
    def closure():
        optimizer.zero_grad()
        loss = loss_fn(alpha, observations, next_observations, rewards, actions, log_likes, q_net, policy)
        loss.backward()
        return loss

    for i in range(steps):
        optimizer.step(closure)

# ALGO LOGIC: initialize agent here:
class QNetwork(nn.Module):
    def __init__(self, env, args):
        super().__init__()
        self.env = env
        self.alpha = args.alpha
        self.critic = nn.Sequential(
            layer_init(nn.Linear(np.array(env.single_observation_space.shape).prod(), 128)),
            nn.ReLU(),
            layer_init(nn.Linear(128, 128)),
            nn.ReLU(),
            layer_init(nn.Linear(128, env.single_action_space.n), std=1),
        )

    def forward(self, x):
        return self.critic(x)
    
    def get_values(self, x, action=None, policy=None):
        q = self(x)
        z = q / self.alpha
        if policy is None: pi_k = torch.ones(x.shape[0], self.env.single_action_space.n, device=x.device) / self.env.single_action_space.n
        else: _, _, _, pi_k = policy.get_action(x); pi_k = pi_k.detach()
        v = self.alpha * (torch.log(torch.sum(pi_k * torch.exp(z), dim=1))).squeeze(-1)
        if action is None:
            return q, v
        else:
            q = q.gather(-1, action.unsqueeze(-1).long()).squeeze(-1)
            return q, v
    
class QREPSPolicy(nn.Module):
    def __init__(self, env):
        super().__init__()
        self.actor = nn.Sequential(
            layer_init(nn.Linear(np.array(env.single_observation_space.shape).prod(), 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, env.single_action_space.n), std=0.01),
        )

    def forward(self, x):
        return self.actor(x)

    def get_action(self, x, action=None):
        logits = self(x)
        policy_dist = Categorical(logits=logits)
        if action is None: action = policy_dist.sample()
        action_probs = policy_dist.probs
        log_prob = torch.log(action_probs+1e-6)
        action_log_prob = policy_dist.log_prob(action)
        return action, action_log_prob, log_prob, action_probs

def main(config):
    import torch
    import torch.nn as nn
    import torch.optim as optim
    import stable_baselines3 as sb3

    # assert args.num_envs == 1, "vectorized envs are not supported at the moment"
    args = argparse.Namespace(**config)
    args.seed = config["__trial_index__"] + SEED_OFFSET
    run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    logging_callback=lambda r: train.report({'reward':r})
    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

    # env setup
    envs = gym.vector.SyncVectorEnv(
        [make_env(args.env_id, args.seed + i, i, args.capture_video, run_name) for i in range(args.num_envs)]
    )
    # assert isinstance(envs.single_action_space, gym.spaces.Discrete), "only discrete action space is supported"

    actor = QREPSPolicy(envs).to(device)
    qf = QNetwork(envs, args).to(device)
    qf2 = QNetwork(envs, args).to(device)

    qf_target = QNetwork(envs, args).to(device)
    qf2_target = QNetwork(envs, args).to(device)

    qf_target.load_state_dict(qf.state_dict())
    qf2_target.load_state_dict(qf2.state_dict())

    q_optimizer = optim.Adam(list(qf.parameters()) + list(qf2.parameters()), lr=args.q_lr_start, eps=1e-4)
    actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.policy_lr_start, eps=1e-4)


    # q_optimizer = optim.SGD(list(qf.parameters()), lr=args.q_lr_start)

    if args.autotune:
        target_entropy = -args.target_entropy_scale * torch.log(1 / torch.tensor(envs.single_action_space.n))
        log_alpha = torch.zeros(1, requires_grad=True, device=device)
        alpha = log_alpha.exp().item()
        a_optimizer = optim.Adam([log_alpha], lr=args.q_lr_start, eps=1e-4)
    else:
        alpha = args.alpha
    if args.eta is None: eta = args.alpha
    else: eta = args.eta

    rb = ReplayBuffer(args.buffer_size)
    start_time = time.time()

    # TRY NOT TO MODIFY: start the game
    global_step = 0
    try:

     for T in range(args.num_updates):
            all_rewards = []
            if args.use_linear_schedule:
                # q_optimizer.param_groups[0]["lr"] = linear_schedule(start_e= args.q_lr_start, end_e=args.q_lr_end, duration=100, t=T)
                # actor_optimizer.param_groups[0]["lr"] = linear_schedule(start_e= args.policy_lr_start, end_e=args.policy_lr_end, duration=100, t=T)
                

                frac = 1.0 - (T - 1.0) / args.num_updates
                lrnow = frac * args.q_lr_start
                q_optimizer.param_groups[0]["lr"] = lrnow

                frac = 1.0 - (T - 1.0) / args.num_updates
                lrnow = frac * args.policy_lr_start
                actor_optimizer.param_groups[0]["lr"] = lrnow
            for N in range(args.num_rollouts):
                obs, _ = envs.reset(seed=args.seed)
                episode_reward = []
                for step in range(args.total_timesteps):
                    global_step += args.num_envs
                    with torch.no_grad():
                        actions, _, loglikes, probs = actor.get_action(torch.Tensor(obs).to(device))
                    
                    # TRY NOT TO MODIFY: execute the game and log data.
                    next_obs, reward, done, truncation, info = envs.step(actions.detach().cpu().numpy())
                    reward, obs, next_obs, done = torch.tensor(reward).to(device).view(-1), torch.Tensor(obs).to(device), torch.Tensor(next_obs).to(device), torch.Tensor(done).to(device)

                    rb.push(obs, next_obs, actions, reward, done, loglikes)
                    obs = next_obs
                    all_rewards.append(reward)
                    episode_reward.append(reward)
                    if done.any():
                        break

            # TRAINING PHASE         
            (
            observations, 
            next_observations, 
            actions, 
            rewards, 
            dones, 
            log_likes
            ) = rb.get_all()

            q_nets = [qf, qf2, qf_target, qf2_target]
            if args.saddle_point_optimization:
                sampler = ExponentiatedGradientSampler(observations.shape[0], device, eta, beta=args.beta)
                optimize_critic(eta, observations, next_observations, actions, rewards, q_nets, actor, args.gamma, sampler, q_optimizer, steps=args.update_epochs, loss_fn=saddle)
            else:
                optimize_critic(eta, observations, next_observations, actions, rewards, q_nets, actor, args.gamma, None, q_optimizer, steps=args.update_epochs, loss_fn=ELBE)

            if args.use_kl_loss: optimize_actor(alpha, observations, next_observations, rewards, actions, log_likes, q_nets, actor, actor_optimizer, steps=args.update_policy_epochs, loss_fn=kl_loss)
            else: optimize_actor(alpha, observations, next_observations, rewards, actions, log_likes, q_nets, actor, actor_optimizer, steps=args.update_policy_epochs, loss_fn=nll_loss)
            
            # print("Step:", T, "reward:", np.sum([rew.cpu().numpy() for rew in all_rewards])/(args.num_rollouts*args.num_envs))
            writer.add_scalar("charts/episodic_return", np.sum([rew.cpu().numpy() for rew in all_rewards])/(args.num_rollouts*args.num_envs), T)
            logging_callback(np.sum([rew.cpu().numpy() for rew in all_rewards])/(args.num_rollouts*args.num_envs))

            rb.reset()
            if args.autotune:
                actions, a_loglike, loglikes, probs = actor.get_action(torch.Tensor(obs).to(device))
                
                # re-use action probabilities for temperature loss
                alpha_loss = (probs.detach() * (-log_alpha.exp() * (loglikes + target_entropy).detach())).mean()

                a_optimizer.zero_grad()
                alpha_loss.backward()
                a_optimizer.step()
                alpha = log_alpha.exp().item()
            
            # update the target networks
            if T % args.target_network_frequency == 0:
                for param, target_param in zip(qf.parameters(), qf_target.parameters()):
                    target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
                for param, target_param in zip(qf2.parameters(), qf2_target.parameters()):
                    target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
    except:
        logging_callback(0.0)

    envs.close()
    writer.close()

ray_init_config = {
    "num_gpus": 1,  # Adjust based on the number of available GPUs
    "num_cpus": 4,  # Number of CPU cores to allocate per trial
    # Additional Ray initialization options if needed
}

search_alg = HEBOSearch(metric="reward", mode="max")
re_search_alg = Repeater(search_alg, repeat=1)

analysis = tune.run(
    main,
    num_samples=1000,
    config=config,
    search_alg=re_search_alg,
    # resources_per_trial=ray_init_config,
    local_dir="/Users/nicolasvila/workplace/uni/tfg_v2/tests/qreps/results_tune_qreps_v3",
)

print("Best config: ", analysis.get_best_config(metric="reward", mode="max"))

# Get a dataframe for analyzing trial results.
df = analysis.results_df

df.to_csv("tuning_2q_norand.csv")