"""A class for cloning an MPC policy using a neural network"""
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Tuple, Optional
import warnings

from cvxpylayers.torch import CvxpyLayer
import torch
import torch.nn as nn
import numpy as np
from .barriernet import BarrierNetLayer
from UMNN.models.UMNN import MonotonicNN

from tqdm import tqdm


class PosLinear(nn.Module):
    def __init__(self, in_dim, out_dim):
        super(PosLinear, self).__init__()
        self.weight = nn.Parameter(torch.randn((in_dim, out_dim)))
        self.bias = nn.Parameter(torch.zeros((out_dim,)))

    def forward(self, x):
        return torch.matmul(x, self.weight) + torch.abs(self.bias)


class PolicyCloningModel(torch.nn.Module):
    def __init__(
            self,
            hidden_layers: int,
            hidden_layer_width: int,
            n_state_dims: int,
            n_control_dims: int,
            n_input_dims: int,
            cbf: List[Callable[[torch.Tensor, torch.Tensor, torch.Tensor], torch.Tensor]],
            # Ordered list of Barrier functions
            n_cbf_slack: int,
            cbf_rel_degree: List[int],
            state_space: List[Tuple[float, float]],
            control_bounds: List[Tuple[float, float]],
            x_obst: torch.Tensor,
            r_obst: torch.Tensor,
            cbf_slack_weight: Optional[List[float]] = None,
            load_from_file: Optional[str] = None,
    ):
        """
        A model for cloning a policy.

        args:
            hidden_layers: number of hidden layers
            hidden_layer_width: width of hidden layers (num neurons per layer)
            n_state_dims: how many input state dimensions
            n_control_dims: how many output control dimensions
            n_input_dims: how many input dimensions to the barrier net (can be different from state dims with at least n_state_dims)
            cbf: List of barrier functions (in order)
            n_cbf_slack: number of CBF slack variables (can be 0)
            cbf_slack_weight: weight for the slack variables for CBF constraints (must be a list of length n_cbf_slack)
            cbf_rel_degree: relative degree of the barrier functions (must be a list of length n_cbfs)
            state_space: list of tuples of (min, max) for each state dimension
            control_bounds: list of tuples of the form (min, max) for each control dimension
            preprocess_barrier_input_fn: function to preprocess the input to the barrier net (must construct matrices for clf and cbf constraints)
            load_from_file: path to a file to load the model from
            
        """
        super(PolicyCloningModel, self).__init__()

        # ----------------- Propagate Class Properties -----------------
        self.hidden_layers = hidden_layers
        self.hidden_layer_width = hidden_layer_width
        self.n_state_dims = n_state_dims
        self.n_control_dims = n_control_dims
        self.n_input_dims = n_input_dims
        self.load_from_file = load_from_file
        self.state_space = state_space
        # Define device
        if torch.cuda.is_available():
            self.device = torch.device("cuda:0")
        else:
            self.device = torch.device("cpu")
        self.to(self.device)

        # ----------------- Construct MLP Network -----------------
        # Compute the output dimension of the MLP
        self.n_output_dims = n_control_dims + sum(cbf_rel_degree)
        self.policy_layers: OrderedDict[str, nn.Module] = OrderedDict()
        self.policy_layers["input_linear"] = nn.Linear(
            self.n_input_dims,
            self.hidden_layer_width,
        )
        self.policy_layers["input_activation"] = nn.Softplus()
        for i in range(self.hidden_layers):
            self.policy_layers[f"layer_{i}_linear"] = nn.Linear(
                self.hidden_layer_width, self.hidden_layer_width
            )
            self.policy_layers[f"layer_{i}_activation"] = nn.Softplus()
        # Output the penalty parameters for cbf
        self.policy_layers["output_linear"] = nn.Linear(
            self.hidden_layer_width, self.n_control_dims
        )
        # Convert to sequential model
        self.policy_nn = nn.Sequential(self.policy_layers).to(self.device)

        # ----------------- Construct CBF Network -----------------
        # Monotonically increasing neural network for relative degree 2
        self.mono1 = MonotonicNN(self.n_state_dims+1, [100]*3, nb_steps=100, dev=self.device).to(self.device)
        self.mono2 = MonotonicNN(self.n_state_dims+1, [100]*3, nb_steps=100, dev=self.device).to(self.device)

        self.n_cbf = len(cbf)
        self.cbf = cbf
        self.x_obst = x_obst.to(self.device)
        self.r_obst = r_obst.to(self.device)
        # ----------------- Construct Barrier Network -----------------
        self.barrier_layer = BarrierNetLayer(
            n_state_dims=self.n_state_dims,
            n_control_dims=self.n_control_dims,
            n_cbf=self.n_cbf,
            n_cbf_slack=n_cbf_slack,
            cbf_slack_weight=cbf_slack_weight,
            cbf_rel_degree=cbf_rel_degree,
            control_bounds=control_bounds,
            device=self.device,
        )

        # Load the weights and biases if provided
        try:
            if load_from_file is not None:
                checkpoint = torch.load(load_from_file, map_location=self.device)
                self.load_state_dict(checkpoint["model_state_dict"])
        except:
            warnings.warn("Failed to load model from file")

    def forward(self, x: torch.Tensor, x_obs: Optional[torch.Tensor], x_des: Optional[torch.Tensor]):
        # Construct the input to the barrier net
        x = torch.atleast_2d(x)
        if x_obs is not None and x_des is not None:
            x_obs = x_obs.repeat(x.shape[0], 1)
            x_des = x_des.repeat(x.shape[0], 1)
            x_in = torch.hstack([x, x_obs, x_des]).to(self.device)
        elif x_obs is not None and x_des is None:
            x_obs = x_obs.repeat(x.shape[0], 1)
            x_in = torch.hstack([x, x_obs]).to(self.device)
        else:
            x_in = x.to(self.device)
        # pass state through policy network
        u_out = self.policy_nn(x_in)
        u_ref = u_out[:, :self.n_control_dims]
        # pass state through cbf network
        return self.barrier_layer(*self.compute_hocbf_params(x, u_ref)), u_ref

    def eval_np(self, x: np.ndarray, x_obs: Optional[np.ndarray] = None, x_des: Optional[np.ndarray] = None):
        # Construct the input to the barrier net

        x = torch.atleast_2d(torch.from_numpy(x)).to(self.device)
        if x_obs is not None and x_des is not None:
            x_obs = x_obs.repeat(x.shape[0], 1).to(self.device)
            x_des = x_des.repeat(x.shape[0], 1).to(self.device)
            x_in = torch.hstack([x, x_obs, x_des]).to(self.device)
        elif x_obs is not None and x_des is None:
            x_obs = x_obs.repeat(x.shape[0], 1).to(self.device)
            x_in = torch.hstack([x, x_obs]).to(self.device)
        else:
            x_in = x.to(self.device)

        u_out = self.policy_nn(x_in)
        u_ref = u_out[:, :self.n_control_dims]
        # pass state through cbf network
        return self.barrier_layer(*self.compute_hocbf_params(x, u_ref)).detach().cpu().squeeze()

    def _f(self, x):
        """Open Loop Dynamics"""
        return torch.vstack([x[2] * torch.cos(x[3]), x[2] * torch.sin(x[3]), torch.zeros(2, 1).to(self.device)])

    def _g(self):
        """ Control Matrix"""
        return torch.vstack([torch.zeros(2, 2).to(self.device), torch.eye(2).to(self.device)])

    def _distance_to_obstacle(self, x):
        return (x[:, 0] - self.x_obst[0])**2 + (x[:, 1] - self.x_obst[1]) ** 2 - self.r_obst ** 2

    def compute_hocbf_params(self, x: torch.Tensor, u_ref: torch.Tensor):
        # Compute CBF parameters
        A_cbf = torch.zeros(self.n_cbf, self.n_control_dims).repeat(x.shape[0], 1, 1).to(self.device)
        b_cbf = torch.zeros(self.n_cbf, 1).repeat(x.shape[0], 1, 1).to(self.device)
        # Distance from Obstacle
        cbf = lambda x: self.cbf[0](x, self.x_obst.reshape(-1), self.r_obst)

        for i in range(x.shape[0]):
            alpha_1 = lambda psi: self.mono1(torch.atleast_2d(psi), torch.atleast_2d(x[i]))
            alpha_2 = lambda psi: self.mono2(torch.atleast_2d(psi), torch.atleast_2d(x[i]))
            A_i, b_i = self._compute_lie_derivative(x[i], cbf, alpha_1, alpha_2)
            A_cbf[i] = -A_i
            b_cbf[i] = b_i
        return u_ref.reshape((x.shape[0], self.n_control_dims, 1)), A_cbf, b_cbf

    def _compute_lie_derivative(self, x: torch.Tensor, barrier_fun: Callable, alpha_fun_1: Callable,
                                alpha_fun_2: Callable):
        """Compute the Lie derivative of the CBF wrt the dynamics"""
        # Make sure the input requires gradient
        x.requires_grad_(True)
        # Compute the CBF
        psi0 = barrier_fun(x)
        db_dx = torch.autograd.grad(psi0, x, create_graph=True, retain_graph=True)[0]
        Lfb = db_dx @ self._f(x)
        db2_dx = torch.autograd.grad(Lfb, x, retain_graph=True)[0]
        Lf2b = db2_dx @ self._f(x)
        LgLfb = db2_dx @ self._g()
        psi1 = Lfb + alpha_fun_1(psi0)
        psi1_dot = torch.autograd.grad(psi1, x, retain_graph=True)[0] @ self._f(x)
        psi2 = psi1_dot + alpha_fun_2(psi1)
        # Compute the Lie derivative
        return LgLfb, Lf2b + psi2

    def _barrier_loss(self, x_train, u_train, batch_size):
        """Compute the barrier loss"""
        # Compute the CBF parameters
        _, A_cbf, b_cbf = self.compute_hocbf_params(x_train, u_train)
        LgLfb = -A_cbf
        # Compute the barrier loss
        loss = 0
        beta1 = 1
        beta2 = 0.01
        for i in range(self.n_cbf):
            # constraint violation
            loss += beta1*torch.sum(torch.relu(-(LgLfb[:, i, :] * u_train + b_cbf[:, i, :])))
            # constraint satisfaction
            loss += -beta2*torch.sum(torch.relu(torch.tanh(LgLfb[:, i, :] * u_train + b_cbf[:, i, :])))
        return loss/batch_size


    def save_to_file(self, save_path: str):
        save_data = {
            "hidden_layers": self.hidden_layers,
            "hidden_layer_width": self.hidden_layer_width,
            "n_state_dims": self.n_state_dims,
            "n_control_dims": self.n_control_dims,
            "state_space": self.state_space,
            "n_output_dims": self.n_output_dims,
            "barrier_net_fn": self.barrier_net_fn,
            "state_dict": self.state_dict(),
        }
        torch.save(save_data, save_path)

    def clone(
            self,
            expert: Callable[[torch.Tensor], torch.Tensor],
            n_pts: int,
            n_epochs: int,
            learning_rate: float,
            batch_size: int = 64,
            save_path: Optional[str] = None,
            load_checkpoint: Optional[str] = None,
            x_obs: Optional[torch.Tensor] = None,
            x_des: Optional[torch.Tensor] = None,
    ):
        """Clone the provided expert policy. Uses dead-simple supervised regression
        to clone the policy (no DAgger currently).

        args:
            expert: the policy to clone
            n_pts: the number of points in the cloning dataset
            n_epochs: the number of epochs to train for
            learning_rate: step size
            batch_size: size of mini-batches
            save_path: path to save the file (if none, will not save the model)
        """
        # Generate some training data
        # Start by sampling points uniformly from the state space
        x_train = torch.zeros((n_pts, self.n_state_dims))
        for dim in range(self.n_state_dims):
            x_train[:, dim] = torch.Tensor(n_pts).uniform_(*self.state_space[dim])

        # Now get the expert's control input at each of those points
        u_expert = torch.zeros((n_pts, self.n_control_dims))
        data_gen_range = tqdm(range(n_pts), ascii=True, desc="Generating data")
        data_gen_range.set_description("Generating training data...")
        for i in data_gen_range:
            u_expert[i, :] = expert(x_train[i, :])

        # Move inputs and outputs to the GPU
        x_train = x_train.to(self.device)
        u_expert = u_expert.to(self.device)

        # Make a loss function and optimizer
        mse_loss_fn = torch.nn.MSELoss(reduction="mean")
        optimizer = torch.optim.Adam(
            self.parameters(), lr=learning_rate
        )
        # Load checkpoint if provided
        if load_checkpoint:
            checkpoint = torch.load(load_checkpoint, map_location=self.device)
            self.load_state_dict(checkpoint["model_state_dict"])
            optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
            epoch = checkpoint["epoch"]
            loss = checkpoint["loss"]
            n_epochs -= epoch
        curr_min_loss = np.inf
        # Optimize in mini-batches
        for epoch in range(n_epochs):
            permutation = torch.randperm(n_pts)

            loss_accumulated = 0.0
            epoch_range = tqdm(range(0, n_pts, batch_size), ascii=True, desc="Epoch")
            epoch_range.set_description(f"Epoch {epoch} training...")
            for i in epoch_range:
                batch_indices = permutation[i: i + batch_size]
                x_batch = x_train[batch_indices]
                u_expert_batch = u_expert[batch_indices]

                # Forward pass: predict the control input
                u_predicted, u_ref_predicted = self(x_batch, x_obs.squeeze(), x_des.squeeze())
                # Compute the loss and backpropagate
                # MSE Loss
                # Clone Loss
                # loss = mse_loss_fn(u_ref_predicted.squeeze(), u_expert_batch)
                loss = mse_loss_fn(u_ref_predicted.squeeze(), u_expert_batch)
                # CBF Loss
                loss += mse_loss_fn(u_predicted.squeeze(), u_expert_batch)
                # loss = mse_loss_fn(u_predicted.squeeze(), u_expert_batch)
                # CBF Loss
                loss += self._barrier_loss(x_batch, u_expert_batch, batch_size)
                # Add L1 regularization
                for layer in self.policy_nn:
                    if not hasattr(layer, "weight"):
                        continue
                    loss += 0.001 * learning_rate * torch.norm(layer.weight, p=2)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                loss_accumulated += loss.detach()

            print(f"Epoch {epoch}: {loss_accumulated / (n_pts / batch_size)}")
            if loss_accumulated < curr_min_loss:
                torch.save({
                    'epoch': epoch,
                    'model_state_dict': self.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'loss': loss_accumulated,
                }, save_path)
                curr_min_loss = loss_accumulated

        # if save_path is not None:
        #     self.save_to_file(save_path)
