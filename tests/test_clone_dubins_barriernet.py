"""Test the obstacle avoidance BarrierNet for a dubins vehicle"""
from typing import Callable, Tuple, List

import numpy as np
import torch
import matplotlib.pyplot as plt
import sys
import os
import dill
import cvxpy as cp
import warnings

sys.path.append(os.path.abspath('..'))

from NNet.converters.onnx2nnet import onnx2nnet

from mpc.costs import lqr_running_cost, squared_error_terminal_cost
from mpc.dynamics_constraints import car_2d_dynamics as dubins_car_dynamics
from mpc.mpc import construct_MPC_problem, solve_MPC_problem
from mpc.obstacle_constraints import hypersphere_sdf
from mpc.simulator import simulate_nn

from mpc.network_utils import pytorch_to_nnet
from cbf.barriernet import BarrierNet
from cvxpylayers.torch import CvxpyLayer

n_states = 4
n_controls = 2
horizon = 15
n_penalty_params = 2
dt = 0.1

# Define dynamics
dynamics_fn = dubins_car_dynamics

# Define obstacles
radius = 0.2
margin = 0.1
center = [-1.0, 0.0]

# Define limits for state space
state_space = [
    (-2.0, 2.0),
    (-2.0, 2.0),
    (-3.0, 3.0),
    (-np.pi, np.pi),
]
# Define control bounds
control_bounds = [0.5, np.pi / 2]
torch.set_default_dtype(torch.float64)
# Define device
if torch.cuda.is_available():
    device = torch.device("cuda:0")
else:
    device = torch.device("cpu")


def define_dubins_mpc_expert() -> Callable[[torch.Tensor], torch.Tensor]:
    """Run a test of obstacle avoidance MPC with a dubins car and return the results"""
    # -------------------------------------------
    # Define the problem
    # -------------------------------------------
    # Define obstacles by defining a signed distance function
    obstacle_fns = [(lambda x: hypersphere_sdf(x, radius, [0, 1], center), margin)]

    # Define costs
    x_goal = np.array([0.0, 0.0, 0.5, 0.0])
    running_cost_fn = lambda x, u: lqr_running_cost(
        x, u, x_goal, dt * np.diag([1.0, 1.0, 0.1, 0.0]), 0.05 * np.eye(2)
    )
    terminal_cost_fn = lambda x: squared_error_terminal_cost(x, x_goal)

    # Define MPC problem
    opti, x0_variables, u0_variables, x_variables, u_variables = construct_MPC_problem(
        n_states,
        n_controls,
        horizon,
        dt,
        dynamics_fn,
        obstacle_fns,
        running_cost_fn,
        terminal_cost_fn,
        control_bounds,
    )

    # -------------------------------------------
    # Wrap the MPC problem to accept a tensor input and tensor output
    # -------------------------------------------
    max_tries = 10

    def mpc_expert(current_state: torch.Tensor) -> torch.Tensor:
        # Initialize counters and variables
        tries = 0
        success = False
        x_guess = None
        u_guess = None

        while not success and tries < max_tries:
            success, control_out, _, _ = solve_MPC_problem(
                opti.copy(),
                x0_variables,
                u0_variables,
                current_state.detach().numpy(),
                x_variables=x_variables,
                u_variables=u_variables,
                x_guess=x_guess,
                u_guess=u_guess,
            )
            tries += 1

            if not success:
                print(f"failed after {tries} tries")

        return torch.from_numpy(control_out)

    return mpc_expert


def define_cbf_filter():
    """ Define a differentiable barrier function for the dubins car """
    # Define the barrier function
    u = cp.Variable(n_controls)  # control input (velocity, steering angle) [m/s, rad]
    u_ref = cp.Parameter(n_controls)  # reference control input (velocity, steering angle) [m/s, rad]
    # Define CBF Parameters
    # r_vals = cp.Parameter(n_controls, nonneg=True)  # Diagonal matrix of the control cost
    LgLfb = cp.Parameter((1, n_controls))  # Gradient of the barrier function
    Lf2b = cp.Parameter(1)  # Second derivative of the barrier function
    p_alpha = cp.Parameter(1)  # Barrier function
    # Define CBF
    cbf_constraint = -LgLfb @ u <= Lf2b + p_alpha

    cbf_objective = cp.sum(cp.square(u-u_ref))
    # Define the CBF and CLF problems
    cbf_problem = cp.Problem(cp.Minimize(cbf_objective), [cbf_constraint])

    return CvxpyLayer(cbf_problem, parameters=[u_ref, LgLfb, Lf2b, p_alpha],
                      variables=[u])

def define_clf_filter():
    """ Define a differentiable barrier function for the dubins car """
    # Define the barrier function
    u = cp.Variable(n_controls)  # control input (velocity, steering angle) [m/s, rad]
    # Define CLF Parameters
    delta = cp.Variable(2)  # slack variable for the barrier function
    r_vals = cp.Parameter(n_controls, nonneg=True)  # Penalty parameters Control diagonal matrix
    # q_vals = cp.Parameter(n_controls, nonneg=True)  # Penalty parameters Slack diagonal matrix
    LfV = cp.Parameter(2)  # Gradient of the value function
    LgV = cp.Parameter(2)  # Gradient of the Lyapunov function along the trajectory
    V = cp.Parameter(2)  # Lyapunov function

    # Define CLF
    clf_constraint = cp.multiply(LgV, u) + LfV + V <= delta
    clf_objective = cp.sum(cp.multiply(r_vals, cp.square(u)))+cp.sum(cp.multiply([3,3], cp.square(delta)))
    # Input constraints
    input_constraint = []
    for control_idx, bound in enumerate(control_bounds):
        input_constraint+=[u[control_idx] <= bound]
        input_constraint+=[u[control_idx] >= -bound]
    # Define the CBF and CLF problems

    clf_problem = cp.Problem(cp.Minimize(clf_objective), [clf_constraint]+input_constraint)

    return CvxpyLayer(clf_problem, parameters=[r_vals, LfV, LgV, V],
                      variables=[u, delta])

def nn_input(x):
    nn_input = torch.zeros(n_states + 1)
    nn_input[:n_states] = x[:n_states]
    nn_input[3] = torch.sin(x[3])
    nn_input[4] = torch.cos(x[3])
    return nn_input

def construct_clf_cbf(parameters: torch.Tensor, clf_layer, x, x_obstacle, x_goal):
    """ Decompose the parameters into the CBF and CLF parameters and defines derivatives and constraint constants for
    cbf and clf filters """
    # Define the CBF parameters
    p_cbf = parameters[:n_penalty_params] # Penalty parameters for class K barrier function
    r_vals_clf = parameters[n_penalty_params:n_penalty_params + n_controls] #  CBF control penalty parameters
    q_vals_clf = parameters[n_penalty_params + n_controls:n_penalty_params + 2 * n_controls] # CLF control penalty parameters
    # Compute CBF values
    LgLfb = torch.reshape(torch.hstack([-2 * (x[0] - x_obstacle[0]) * x[2] * torch.sin(x[3]) +
                                        2 * (x[1] - x_obstacle[1]) * x[2] * torch.cos(x[3]),
                                        2 * (x[0] - x_obstacle[0]) * torch.cos(x[3]) +
                                        2 * (x[1] - x_obstacle[1]) * torch.sin(x[3])]), (1, n_controls))
    Lf2b = torch.reshape(2 * torch.square(x[2]), (1, 1))
    Lfb = 2 * (x[0] - x_obstacle[0]) * x[2] * torch.cos(x[3]) + 2 * (x[1] - x_obstacle[1]) * x[2] * torch.sin(x[3])
    b = torch.square(x[0] - x_obstacle[0])+torch.square(x[1]-x_obstacle[1]) - radius ** 2
    p_alpha = torch.reshape((p_cbf[0] + p_cbf[1]) * Lfb + p_cbf[1] * p_cbf[0] * b, (1, 1))
    # Compute CLF values
    V_speed = torch.square(x[2] - x_goal[2])
    # Compute des angle
    V_angle = torch.square(torch.cos(x[3])*(x[1]-x_goal[1])-torch.sin(x[3])*(x[0]-x_goal[0]))

    LfV = torch.zeros(2).to(device).requires_grad_(False)
    LgV = torch.reshape(torch.vstack([2 * (x[2] - x_goal[2]),
                                      -2*(torch.cos(x[3])*(x[0] - x_goal[0]) +torch.sin(x[3])*(x[1] - x_goal[1]))
                                      *(torch.cos(x[3])*(x[1] - x_goal[1]) - torch.sin(x[3])*(x[0] - x_goal[0]))]),(2,))
    V = torch.reshape(torch.vstack([V_speed, V_angle]),(2,))*q_vals_clf
    # Compute Reference Control
    try:
        u_ref = clf_layer(r_vals_clf, LfV, LgV, V)[0]
    except Exception as exp:
        warnings.warn("Problem raised exception: {}".format(exp))
        u_ref = torch.zeros(n_controls).to(device)

    return u_ref, LgLfb, Lf2b, p_alpha


def clone_dubins_barrier_preferences(train=True, load=False):
    # Define Barrier Function
    barrier_fn = define_cbf_filter()
    clf_fn = define_clf_filter()
    x_obstacle = torch.tensor(center).to(device).requires_grad_(False)
    x_goal = torch.tensor([0.0, 0.0, 0.5, 0.0]).to(device).requires_grad_(False)
    barrier_policy_fn = lambda x, p: barrier_fn(*construct_clf_cbf(p, clf_fn, x, x_obstacle, x_goal))[0]
    # -------------------------------------------
    # Clone the MPC policy
    # -------------------------------------------
    mpc_expert = define_dubins_mpc_expert()
    hidden_layers = 4
    hidden_layer_width = 32
    cloned_policy = BarrierNet(
        hidden_layers= hidden_layers,
        hidden_layer_width= hidden_layer_width,
        n_state_dims= n_states,
        n_control_dims= n_controls,
        n_input_dims= n_states,
        n_output_dims= n_penalty_params + 2 * n_controls,
        state_space=state_space,
        barrier_net_fn=barrier_policy_fn,
        preprocess_input_fn=None,
        # load_from_file="mpc/tests/data/cloned_quad_policy_weight_decay.pth",
    )

    n_pts = int(1e4)
    n_epochs = 500
    learning_rate = 1e-3
    # Define Training optimizer
    if train and not load:
        cloned_policy.clone(
            mpc_expert,
            n_pts,
            n_epochs,
            learning_rate,
            batch_size=32,
            save_path="./data/cloned_dubins_barrier_policy_weight_decay.pt",
        )
    elif train and load:
        checkpoint = "./data/cloned_dubins_barrier_policy_weight_decay.pt"
        cloned_policy.clone(
            mpc_expert,
            n_pts,
            n_epochs,
            learning_rate,
            save_path="./data/cloned_dubins_barrier_policy_weight_decay.pt",
            load_checkpoint=checkpoint,
        )
    else:
        load_checkpoint = "./data/cloned_dubins_barrier_policy_weight_decay.pt"
        checkpoint = torch.load(load_checkpoint, map_location=device)
        cloned_policy.load_state_dict(checkpoint["model_state_dict"])

    return cloned_policy


def simulate_and_plot(policy):
    # -------------------------------------------
    # Simulate the cloned policy
    # -------------------------------------------

    # Define initial states
    x0s = [
        np.array([-2.0, 0.0, 0.0, 0.0]),
        np.array([-2.0, 0.1, 0.0, 0.0]),
        np.array([-2.0, 0.2, 0.0, 0.0]),
        np.array([-2.0, 0.5, 0.0, 0.0]),
        np.array([-2.0, -0.1, 0.0, 0.0]),
        np.array([-2.0, -0.2, 0.0, 0.0]),
        np.array([-2.0, -0.5, 0.0, 0.0]),
    ]

    fig = plt.figure(figsize=plt.figaspect(1.0))
    ax = fig.add_subplot(1, 1, 1)
    ax.plot([], [], "ro", label="Start")

    n_steps = 100
    for x0 in x0s:
        # Run the cloned policy
        _, x, u = simulate_nn(
            policy,
            x0,
            dt,
            dynamics_fn,
            n_steps,
            substeps=10,
        )

        # Plot it
        ax.plot(x0[0], x0[1], "ro")
        ax.plot(x[:, 0], x[:, 1], "r-")

    # Plot obstacle
    radius = 0.2
    margin = 0.01
    center = [-1.0, 0.0]
    theta = np.linspace(0, 2 * np.pi, 100)
    obs_x = radius * np.cos(theta) + center[0]
    obs_y = radius * np.sin(theta) + center[1]
    margin_x = (radius + margin) * np.cos(theta) + center[0]
    margin_y = (radius + margin) * np.sin(theta) + center[1]
    ax.plot(obs_x, obs_y, "k-")
    ax.plot(margin_x, margin_y, "k:")

    ax.set_xlabel("x")
    ax.set_ylabel("y")

    ax.set_xlim([-2.5, 0.5])
    ax.set_ylim([-1.0, 1.0])
    ax.title.set_text("Cloned Dubins Car Policy")

    ax.set_aspect("equal")

    ax.legend()

    plt.show()

if __name__ == "__main__":
    policy = clone_dubins_barrier_preferences(train= True, load=True)
    simulate_and_plot(policy)
