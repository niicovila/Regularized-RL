import torch
from torch import nn
from torch import distributions
from torch.nn import functional as F
from functools import partial
from torch.distributions import Categorical

from .common import MLP, LinearEnsemble, EnsembleMLP

def weight_init(m, gain=1):
    if isinstance(m, nn.Linear):
        nn.init.orthogonal_(m.weight.data, gain=gain)
        if hasattr(m.bias, 'data'):
            m.bias.data.fill_(0.0)
    if isinstance(m, LinearEnsemble):
        for i in range(m.ensemble_size):
            # Orthogonal initialization doesn't care about which axis is first
            # Thus, we can just use ortho init as normal on each matrix.
            nn.init.orthogonal_(m.weight.data[i], gain=gain)
        if hasattr(m.bias, 'data'):
            m.bias.data.fill_(0.0)

class MLPEncoder(nn.Module):
    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, ortho_init=False):
        assert len(hidden_layers) > 1, "Must have at least one hidden layer for a shared MLP Extractor"
        super().__init__()
        self.mlp = MLP(observation_space.shape[0], output_dim=128, act=act)
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0

    def forward(self, obs):
        return self.mlp(obs)
        
class ContinuousMLPCritic(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, num_q_fns=2, ortho_init=False, output_gain=None):
        super().__init__()
        self.num_q_fns = num_q_fns
        if self.num_q_fns > 1:
            self.q = EnsembleMLP(observation_space.shape[0] + action_space.shape[0], 1, ensemble_size=num_q_fns, hidden_layers=hidden_layers, act=act)
        else:
            self.q = MLP(observation_space.shape[0] + action_space.shape[0], 1, hidden_layers=hidden_layers, act=act)
        
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))

    def forward(self, obs, action):
        x = torch.cat((obs, action), dim=-1)
        q = self.q(x).squeeze(-1) # Remove the last dim
        if self.num_q_fns == 1:
            q = q.unsqueeze(0) # add in the ensemble dim
        return q

class DiscreteMLPCritic(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, ortho_init=False, output_gain=None):
        super().__init__()
        self.q = MLP(observation_space.shape[0], action_space.n, hidden_layers=hidden_layers, act=act, output_act=nn.Tanh)
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
    
    def forward(self, obs):
        return self.q(obs)

    def predict(self, obs):
        q = self(obs)
        action = q.argmax(dim=-1)
        return action
    
class MLPValue(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, ortho_init=False, output_gain=None, output_act=None):
        super().__init__()
        self.mlp = MLP(observation_space.shape[0], 1, hidden_layers=hidden_layers, act=act, output_act=output_act)
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
        
    def forward(self, obs):
        return self.mlp(obs).squeeze(-1) # Return only scalar values, no final dim

class IntegratedValue(nn.Module):
    def __init__(self, observation_space, action_space, alpha, discrete_critic: DiscreteMLPCritic, hidden_layers=[256, 256], act=nn.ReLU, ortho_init=False, output_gain=None, output_act=None):
        super().__init__()
        self.alpha = alpha
        self.discrete_critic = discrete_critic
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
    def forward(self, obs):
        q_value = self.q_function(obs) * self.alpha
        return torch.logsumexp(q_value, dim=-1) / self.alpha 

class ContinuousMLPActor(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, output_act=nn.Tanh, ortho_init=False, output_gain=None):
        super().__init__()
        self.mlp = MLP(observation_space.shape[0], action_space.shape[0], hidden_layers=hidden_layers, act=act, output_act=output_act)
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
        
    def forward(self, obs):
        return self.mlp(obs)
    
class DiscreteMLPActor(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, output_act=nn.Softmax, ortho_init=False, output_gain=None):
        super().__init__()
        self.mlp = MLP(observation_space.shape[0], action_space.n, hidden_layers=hidden_layers, act=act, output_act=output_act)
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
        
    def forward(self, obs):
        return self.mlp(obs)
    
    def predict(self, obs):
        actions = self(obs)
        action = actions.argmax(dim=-1)
        return action
    
class DiscreteMLPActorV2(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, output_act=nn.Tanh, ortho_init=False, output_gain=None):
        super().__init__()
        self.mlp = MLP(observation_space.shape[0], action_space.n, hidden_layers=hidden_layers, act=act, output_act=output_act)
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
    
    def forward(self, obs):
        action_probs = F.softmax(self.mlp(obs), dim=1)
        action_dist = Categorical(action_probs)
        actions = action_dist.sample().view(-1)
        z = (action_probs == 0.0).float() * 1e-8
        log_action_probs = torch.log(action_probs + z)

        return actions, action_probs, log_action_probs
    
    def predict(self, obs, sample=False):
        h = self.mlp(obs)
        out = F.softmax(h, dim=1)
        actions = torch.argmax(
            out, dim=1)
        if sample:
            action_dist = Categorical(out)
            actions = action_dist.sample().view(-1)
        return actions



class SquashedNormal(distributions.TransformedDistribution):

    def __init__(self, loc, scale):
        self._loc = loc
        self.scale = scale
        self.base_dist = distributions.Normal(loc, scale)
        transforms = [distributions.transforms.TanhTransform(cache_size=1)]
        super().__init__(self.base_dist, transforms)

    @property
    def loc(self):
        loc = self._loc
        for transform in self.transforms:
            loc = transform(loc)
        return loc

class DiagonalGaussianMLPActor(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, ortho_init=False,
                       output_gain=None, log_std_bounds=[-5, 2], state_dependent_log_std=True):
        super().__init__()
        self.state_dependent_log_std = state_dependent_log_std
        self.log_std_bounds = log_std_bounds
        if log_std_bounds is not None:
            assert log_std_bounds[0] < log_std_bounds[1]
        
        if self.state_dependent_log_std:
            self.mlp = MLP(observation_space.shape[0], 2*action_space.shape[0], hidden_layers=hidden_layers, act=act, output_act=None)
        else:
            self.mlp = MLP(observation_space.shape[0], action_space.shape[0], hidden_layers=hidden_layers, act=act, output_act=None)
            self.log_std = nn.Parameter(torch.zeros(action_space.shape[0]), requires_grad=True) # initialize a single parameter vector
        
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
        self.action_range = [float(action_space.low.min()), float(action_space.high.max())]
        
    def forward(self, obs):
        if self.state_dependent_log_std:
            mu, log_std = self.mlp(obs).chunk(2, dim=-1)
        else:
            mu, log_std = self.mlp(obs), self.log_std
        
        if self.log_std_bounds is not None:
            log_std = torch.tanh(log_std)
            log_std_min, log_std_max = self.log_std_bounds
            log_std = log_std_min + 0.5 * (log_std_max - log_std_min) * (log_std + 1)
            dist_class = SquashedNormal
        else:
            dist_class = distributions.Normal
        
        dist = dist_class(mu, log_std.exp())
        return dist

    def predict(self, obs, sample=False):
        dist = self(obs)
        if sample:
            action = dist.sample()
        else:
            action = dist.loc
        action = action.clamp(*self.action_range)
        return action
    
class DiagonalGaussianMLPActorDiscrete(nn.Module):

    def __init__(self, observation_space, action_space, hidden_layers=[256, 256], act=nn.ReLU, ortho_init=False,
                       output_gain=None, log_std_bounds=[-5, 2], state_dependent_log_std=True):
        super().__init__()
        self.state_dependent_log_std = state_dependent_log_std
        self.log_std_bounds = log_std_bounds
        if log_std_bounds is not None:
            assert log_std_bounds[0] < log_std_bounds[1]
        
        if self.state_dependent_log_std:
            # self.mlp = MLP(observation_space.shape[0], 2*action_space.shape[0], hidden_layers=hidden_layers, act=act, output_act=None)
            self.mlp = MLP(observation_space.shape[0], 2*action_space.n, hidden_layers=hidden_layers, act=act, output_act=None)
        else:
            self.mlp = MLP(observation_space.shape[0], action_space.shape[0], hidden_layers=hidden_layers, act=act, output_act=None)
            self.log_std = nn.Parameter(torch.zeros(action_space.shape[0]), requires_grad=True) # initialize a single parameter vector
        
        if ortho_init:
            self.apply(partial(weight_init, gain=float(ortho_init))) # use the fact that True converts to 1.0
            if output_gain is not None:
                self.mlp.last_layer.apply(partial(weight_init, gain=output_gain))
        self.action_range = range(action_space.n)
        self.action_range = [self.action_range[0], self.action_range[-1]]
        
    def forward(self, obs):
        if self.state_dependent_log_std:
            mu, log_std = self.mlp(obs).chunk(2, dim=-1)
        else:
            mu, log_std = self.mlp(obs), self.log_std
        
        if self.log_std_bounds is not None:
            log_std = torch.tanh(log_std)
            log_std_min, log_std_max = self.log_std_bounds
            log_std = log_std_min + 0.5 * (log_std_max - log_std_min) * (log_std + 1)
            dist_class = SquashedNormal
        else:
            dist_class = distributions.Normal
        
        dist = dist_class(mu, log_std.exp())
        return dist

    def predict(self, obs, sample=False):
        dist = self(obs)
        if sample:
            action = dist.sample()
        else:
            action = dist.loc
        action = action.clamp(*self.action_range)
        return torch.argmax(action, dim=-1)
