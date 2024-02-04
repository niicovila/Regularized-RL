import logging
import sys, os

from ray import tune
from ray.tune.search import Repeater
from ray.tune.search.hebo import HEBOSearch
import cv2
sys.path.append("../")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


import gym
import torch
from bsuite.utils import gym_wrapper
from torch.utils.tensorboard import SummaryWriter

from qreps.algorithms import QREPS, SaddleQREPS
from qreps.algorithms.sampler import ExponentiatedGradientSampler
from qreps.algorithms.sampler import BestResponseSampler
from qreps.feature_functions import IdentityFeature
from qreps.policies.qreps_policy import QREPSPolicy
from qreps.utilities.trainer import Trainer
from qreps.utilities.util import set_seed
from qreps.valuefunctions import NNQFunction, DiscreteMLPCritic, ResNet

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

FORMAT = "[%(asctime)s]: %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

SEED_OFFSET = 0

qreps_config = {
    "eta": 0.1,
    "beta": 2e-2,
    "saddle_point_steps": 300,
    "policy_opt_steps": 300,
    "discount": 0.99,
    "device": "cuda" if torch.cuda.is_available() else "cpu",
}


def train(config: dict):

    env = gym.make("ALE/Alien-v5")
    num_obs = env.observation_space.shape[0]
    num_act = env.action_space.n
    device = config["device"]

    q_function = ResNet(observation_space=num_obs, action_space=num_act, feature_fn=IdentityFeature()).to(device)
    policy = QREPSPolicy(q_function=q_function, temp=config["eta"])
    writer = SummaryWriter()

    agent = QREPS(
        writer=writer,
        policy=policy,
        q_function=q_function,
        learner=torch.optim.Adam,
        sampler=BestResponseSampler,
        optimize_policy=False,
        reward_transformer=lambda r: r ,
        **config,
    )

    trainer = Trainer()
    trainer.setup(agent, env)
    trainer.train(num_iterations=1, max_steps=20, number_rollouts=2)


train(qreps_config)

