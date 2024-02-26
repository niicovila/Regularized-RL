import torch
import numpy as np
import itertools

import torch.distributions as D
from .base import Algorithm
from research.networks.base import ActorCriticValuePolicy
from research.utils import utils
from functools import partial
import torch.nn.functional as F
import wandb
from torch.utils.tensorboard import SummaryWriter

def gumbel_log_loss_v3(pred, label, beta, clip):
    assert pred.shape == label.shape, "Shapes were incorrect"
    z = (label - pred)/beta
    if clip is not None:
        z = torch.clamp(z, -clip, clip)
    loss = torch.exp(z)
    loss_2 =  z + 1
    loss = loss.mean() #- loss_2.mean()
    return torch.log(loss) #* beta

def mse_loss(pred, label):
    return ((label - pred)**2).mean()

def qreps_loss(pred, label, eta, V, discount, clip = None):
    assert pred.shape == label.shape, "Shapes were incorrect"
    z = (label - pred) * eta
    if clip is not None:
        z = torch.clamp(z, -clip, clip)
    loss = (1/eta) * torch.log((torch.exp(z)).mean())
    loss += (1 - discount) * V.mean()
    return loss.mean()

class Sampler:
    def __init__(self, N, beta=0.001):
        self.n = N
        self.beta_hat = beta
        self.prob_dist = D.Categorical( probs=torch.ones(N) / N)
        self.entropy = self.prob_dist.entropy()
    def probs(self):
        return self.prob_dist.probs
    def update(self, beta, label, pred):
        log_probs = F.log_softmax(self.prob_dist.logits)
        h = (label - pred) - beta*np.log(self.n * self.prob_dist.probs)
        probs = F.softmax(self.beta_hat*h + log_probs, dim=0)
        probs = torch.clamp(probs, min=1e-8, max=1.0)
        self.prob_dist = D.Categorical(probs=probs)

def S(pred, label, sampler, values, beta, discount):
    
    z = label - pred
    dual = sampler.probs() * z 
    errors = dual.sum() - (sampler.entropy + np.log((sampler.n)))*beta
    # errors +=  (1-discount) * values.mean()
    return errors



class QREPSContinuous(Algorithm):

    def __init__(self, env, network_class, dataset_class, 
                       tau=0.005,
                       init_temperature=0.1,
                       critic_freq=1,
                       value_freq=1,
                       actor_freq=2,
                       target_freq=2,
                       env_freq=1,
                       init_steps=1000,
                       alpha=None,
                       exp_clip=10,
                       beta=1.0,
                       loss="gumbel",
                       value_action_noise=0.0,
                       use_value_log_prob=False,
                       max_grad_norm=None,
                       max_grad_value=None,
                       **kwargs):
        '''
        Note that regular SAC (with value function) is recovered by loss="mse" and use_value_log_prob=True
        '''
        # Save values needed for optim setup.
        self.init_temperature = init_temperature
        self._alpha = alpha

        # Initialize wandb
        run_name = f'qreps_{beta}'
        # wandb.init(
        #     project='QREPS_CONTINUOUS',
        #     entity=None,
        #     sync_tensorboard=True,
        #     name=run_name,
        #     monitor_gym=True,
        #     save_code=True,
        # )
        self.writer = SummaryWriter(f"runs/{run_name}")
        super().__init__(env, network_class, dataset_class, **kwargs)

        # Save extra parameters
        self.tau = tau
        self.target_noise = 0.2
        self.noise_clip = 0.5
        self.critic_freq = critic_freq
        self.value_freq = value_freq
        self.actor_freq = actor_freq
        self.target_freq = target_freq
        self.env_freq = env_freq
        self.init_steps = init_steps
        self.exp_clip = exp_clip
        self.beta = beta
        self.loss = loss
        self.value_action_noise = value_action_noise
        self.use_value_log_prob = use_value_log_prob
        self.action_range = (self.env.action_space.low, self.env.action_space.high)
        self.action_range_tensor = utils.to_device(utils.to_tensor(self.action_range), self.device)
        # Gradient clipping
        self.max_grad_norm = max_grad_norm
        self.max_grad_value = max_grad_value



    @property
    def alpha(self):
        return self.log_alpha.exp()
        
    def setup_network(self, network_class, network_kwargs):
        self.network = network_class(self.env.observation_space, self.env.action_space, 
                                     **network_kwargs).to(self.device)
        self.target_network = network_class(self.env.observation_space, self.env.action_space, 
                                     **network_kwargs).to(self.device)
        self.target_network.load_state_dict(self.network.state_dict())
        for param in self.target_network.parameters():
            param.requires_grad = False

    def setup_optimizers(self, optim_class, optim_kwargs):
        # Default optimizer initialization
        self.optim['actor'] = optim_class(self.network.actor.parameters(), **optim_kwargs)
        # Update the encoder with the critic.
        critic_params = itertools.chain(self.network.critic.parameters(), self.network.encoder.parameters())        
        self.optim['critic'] = optim_class(critic_params, **optim_kwargs)
        # self.optim['value'] = optim_class(self.network.value.parameters(), **optim_kwargs)

        self.target_entropy = -np.prod(self.env.action_space.low.shape)
        if self._alpha is None:
            # Setup the learned entropy coefficients. This has to be done first so its present in the setup_optim call.
            self.log_alpha = torch.tensor(np.log(self.init_temperature), dtype=torch.float).to(self.device)
            self.log_alpha.requires_grad = True
            self.optim['log_alpha'] = optim_class([self.log_alpha], **optim_kwargs)
        else:
            self.log_alpha = torch.tensor(np.log(self._alpha), dtype=torch.float).to(self.device)
            self.log_alpha.requires_grad = False
    
    def _step_env(self):
        # Step the environment and store the transition data.
        metrics = dict()
        if self._env_steps < self.init_steps:
            action = self.env.action_space.sample()
        else:
            self.eval_mode()
            with torch.no_grad():
                action = self.predict(self._current_obs, sample=True)
            self.train_mode()
        
        action = np.clip(action, self.env.action_space.low, self.env.action_space.high)
        next_obs, reward, done, info = self.env.step(action)
        self._episode_length += 1
        self._episode_reward += reward

        if 'discount' in info:
            discount = info['discount']
        elif hasattr(self.env, "_max_episode_steps") and self._episode_length == self.env._max_episode_steps:
            discount = 1.0
        else:
            discount = 1 - float(done)

        # Store the consequences.
        self.dataset.add(next_obs, action, reward, done, discount)

        if done:
            self._num_ep += 1
            # update metrics
            metrics['reward'] = self._episode_reward
            metrics['length'] = self._episode_length
            metrics['num_ep'] = self._num_ep
            # Reset the environment
            self._current_obs = self.env.reset()
            self.dataset.add(self._current_obs) # Add the first timestep
            self._episode_length = 0
            self._episode_reward = 0
        else:
            self._current_obs = next_obs

        self._env_steps += 1
        metrics['env_steps'] = self._env_steps
        return metrics

    def _setup_train(self):
        self._current_obs = self.env.reset()
        self._episode_reward = 0
        self._episode_length = 0
        self._num_ep = 0
        self._env_steps = 0
        self.dataset.add(self._current_obs) # Store the initial reset observation!
        self.sampler = Sampler(self.dataset.batch_size)


    def _train_step(self, batch):
        all_metrics = {}

        if self.steps % self.env_freq == 0 or self._env_steps < self.init_steps:
            # step the environment with freq env_freq or if we are before learning starts
            metrics = self._step_env()
            all_metrics.update(metrics)
            if self._env_steps < self.init_steps:
                return all_metrics # return here.
        
        if 'obs' not in batch:
            return all_metrics
        
        batch['obs'] = self.network.encoder(batch['obs'])
        with torch.no_grad():
            batch['next_obs'] = self.target_network.encoder(batch['next_obs'])

        if self.steps % self.critic_freq == 0:
            with torch.no_grad():
                net = self.target_network 
                noisy_next_action = net.actor(batch['next_obs']).rsample()
                target_q = self.target_network.critic(batch['next_obs'], noisy_next_action)
                target_q = torch.min(target_q, dim=0)[0]
                closed_solution_v = (self.beta * torch.log(torch.exp(target_q/self.beta)))
                target_q = batch['reward'] + batch['discount'] * closed_solution_v

                print(closed_solution_v.mean())

            qs = self.network.critic(batch['obs'], batch['action'])

            loss_fn = partial(S, sampler=self.sampler, values=closed_solution_v, beta=self.beta, discount=self.dataset.discount)
            q_loss = sum([loss_fn(qs[i], target_q) for i in range(qs.shape[0])])
            q_p = torch.min(qs, dim=0)[0]
            self.sampler.update(self.beta, target_q, q_p.detach())
    
            self.optim['critic'].zero_grad(set_to_none=True)
            q_loss.backward()
            if self.max_grad_value is not None:
                torch.nn.utils.clip_grad_value_(self.network.critic.parameters(), self.max_grad_value)
            self.optim['critic'].step()

            all_metrics['q_loss'] = q_loss.item()
            all_metrics['target_q'] = target_q.mean().item()

        # Get the Q value of the current policy. This is used for the value and actor
        dist = self.network.actor(batch['obs'])  
        action = dist.rsample()
        log_prob = dist.log_prob(action).sum(dim=-1)
        qs_pi = self.network.critic(batch['obs'], action)
        q_pred = torch.min(qs_pi, dim=0)[0]

        if self.steps % self.actor_freq == 0:
            # Actor Loss
            actor_loss = (self.alpha.detach() * log_prob - q_pred).mean()

            self.optim['actor'].zero_grad(set_to_none=True)
            actor_loss.backward()
            self.optim['actor'].step()

            # Alpha Loss
            if self._alpha is None:
                # Update the learned temperature
                self.optim['log_alpha'].zero_grad(set_to_none=True)
                alpha_loss = (self.alpha * (-log_prob - self.target_entropy).detach()).mean()
                alpha_loss.backward()
                self.optim['log_alpha'].step()
                all_metrics['alpha_loss'] = alpha_loss.item()
        
            all_metrics['actor_loss'] = actor_loss.item()
            all_metrics['entropy'] = (-log_prob.mean()).item()
            all_metrics['alpha'] = self.alpha.detach().item()
        
        if self.steps % self.target_freq == 0:
            # Only update the critic and encoder for speed. Ignore the actor.
            with torch.no_grad():
                for param, target_param in zip(self.network.encoder.parameters(), self.target_network.encoder.parameters()):
                    target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)
                for param, target_param in zip(self.network.critic.parameters(), self.target_network.critic.parameters()):
                    target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

        self.writer.add_scalar("Losses/Q Loss", q_loss.item(), self.steps)
        return all_metrics

    def _validation_step(self, batch):
        raise NotImplementedError("RL Algorithm does not have a validation dataset.")
