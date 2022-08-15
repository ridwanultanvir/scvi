from ctypes import sizeof
from typing import Iterable, Optional

import torch
import torch.nn.functional as F
from torch.distributions import Dirichlet, Normal
import pandas as pd

from scvi import _CONSTANTS
from scvi.distributions import NegativeBinomial
from scvi.module._utils import one_hot
from scvi.module.base import BaseModuleClass, LossRecorder, auto_move_data

LOWER_BOUND = 1e-10
THETA_LOWER_BOUND = 1e-20
B = 10



class CellAssignModule(BaseModuleClass):
    """
    Model for CellAssign.

    Parameters
    ----------
    n_genes
        Number of input genes
    n_labels
        Number of input cell types
    rho
        Binary matrix of cell type markers
    basis_means
        Basis means numpy array
    b_g_0
        Base gene expression tensor. If `None`, use randomly
        initialized `b_g_0`.
    random_b_g_0
        Override to enforce randomly initialized `b_g_0`. If `True`, use
        random default, if `False` defaults to `b_g_0`.
    n_batch
        Number of batches, if 0, no batch correction is performed.
    n_cats_per_cov
        Number of categories for each extra categorical covariate
    n_continuous_cov
        Number of continuous covariates
    """

    def __init__(
        self,
        n_genes: int,
        rho: torch.Tensor,
        basis_means: torch.Tensor,
        b_g_0: Optional[torch.Tensor] = None,
        random_b_g_0: bool = True,
        n_batch: int = 0,
        n_cats_per_cov: Optional[Iterable[int]] = None,
        n_continuous_cov: int = 0,       
    ):
        super().__init__()
        self.n_genes = n_genes
        self.n_labels = rho.shape[1]
        self.n_batch = n_batch
        self.n_cats_per_cov = n_cats_per_cov
        self.n_continuous_cov = n_continuous_cov

        design_matrix_col_dim = n_batch + n_continuous_cov
        design_matrix_col_dim += 0 if n_cats_per_cov is None else sum(n_cats_per_cov)
        self.register_buffer("rho", rho) #Buffers, by default, are persistent and will be saved alongside parameters.

        # perform all other initializations
        self.min_delta = 2
        #self.min_delta = 0
        dirichlet_concentration = torch.tensor([1e-2] * self.n_labels)
        self.register_buffer("dirichlet_concentration", dirichlet_concentration)
        self.shrinkage = True
        if b_g_0 is None or random_b_g_0 is True:
            self.b_g_0 = torch.nn.Parameter(torch.randn(n_genes))
        else:
            self.b_g_0 = torch.nn.Parameter(b_g_0)

        # compute theta
        self.theta_logit = torch.nn.Parameter(torch.randn(self.n_labels))

        # compute delta (cell type specific overexpression parameter)
        # will be clamped by callback during training
        self.delta_log = torch.nn.Parameter(
            torch.FloatTensor(self.n_genes, self.n_labels).uniform_(-2, 2)
        )

        # shrinkage prior on delta
        self.delta_log_mean = torch.nn.Parameter(
            torch.zeros(
                1,
            )
        )
        self.delta_log_log_scale = torch.nn.Parameter(
            torch.zeros(
                1,
            )
        )

        self.log_a = torch.nn.Parameter(torch.zeros(B))

        design_matrix_col_dim += 0 if n_cats_per_cov is None else sum(n_cats_per_cov)
        if design_matrix_col_dim == 0:
            self.beta = None
        else:
            beta_init = torch.zeros(self.n_genes, design_matrix_col_dim)  # (g, p)
            self.beta = torch.nn.Parameter(beta_init)  # (g, p)

        self.register_buffer("basis_means", torch.tensor(basis_means))

    def _get_inference_input(self, tensors):
        return {}

    def _get_generative_input(self, tensors, inference_outputs):
        x = tensors[_CONSTANTS.X_KEY]
        size_factor = tensors["_size_factor"]

        to_cat = []
        if self.n_batch > 0:
            to_cat.append(one_hot(tensors[_CONSTANTS.BATCH_KEY], self.n_batch))

        cont_key = _CONSTANTS.CONT_COVS_KEY
        if cont_key in tensors.keys():
            to_cat.append(tensors[cont_key])

        cat_key = _CONSTANTS.CAT_COVS_KEY
        if cat_key in tensors.keys():
            for cat_input, n_cat in zip(
                torch.split(tensors[cat_key], 1, dim=1), self.n_cats_per_cov
            ):
                to_cat.append(one_hot(cat_input, n_cat))

        design_matrix = torch.cat(to_cat) if len(to_cat) > 0 else None

        input_dict = dict(x=x, size_factor=size_factor, design_matrix=design_matrix)
        return input_dict

    @auto_move_data
    def inference(self):
        return {}

    
    @auto_move_data
    def generative(self, x, size_factor, design_matrix=None):
        #real_marker_gene_mat = pd.read_csv('data/FL_celltype.csv', index_col=0)
        #real_rho = torch.Tensor(real_marker_gene_mat.to_numpy())
        # x has shape (n, g)
        delta = torch.exp(self.delta_log)  # (g, c)
        theta_log = F.log_softmax(self.theta_logit, dim=-1)  # (c)

        # compute mean of NegBin - shape (n_cells, n_genes, n_labels)
        n_cells = size_factor.shape[0]
        base_mean = torch.log(size_factor)  # (n, 1)
        base_mean = base_mean.unsqueeze(-1).expand(
            n_cells, self.n_genes, self.n_labels
        )  # (n, g, c)

        # compute beta (covariate coefficent)
        # design_matrix has shape (n,p)
        if design_matrix is not None:
            covariates = torch.einsum("np,gp->gn", design_matrix, self.beta)  # (g, n)
            covariates = torch.transpose(covariates, 0, 1).unsqueeze(-1)  # (n, g, 1)
            covariates = covariates.expand(n_cells, self.n_genes, self.n_labels)
            base_mean = base_mean + covariates

        # base gene expression
        b_g_0 = self.b_g_0.unsqueeze(-1).expand(n_cells, self.n_genes, self.n_labels)
        delta_rho = delta * self.rho #delta = parameter, rho = marker gene matrix er 0, 1 values
        #delta_rho = delta * real_rho #delta = parameter, rho = marker gene matrix er 0, 1 values
        delta_rho = delta_rho.expand(n_cells, self.n_genes, self.n_labels)  # (n, g, c)
        log_mu_ngc = base_mean + delta_rho + b_g_0
        mu_ngc = torch.exp(log_mu_ngc)  # (n, g, c)

        # compute phi of NegBin - shape (n_cells, n_genes, n_labels)
        a = torch.exp(self.log_a)  # (B)
        a = a.expand(n_cells, self.n_genes, self.n_labels, B)
        b_init = 2 * ((self.basis_means[1] - self.basis_means[0]) ** 2)
        b = torch.exp(torch.ones(B, device=x.device) * (-torch.log(b_init)))  # (B)
        b = b.expand(n_cells, self.n_genes, self.n_labels, B)
        mu_ngcb = mu_ngc.unsqueeze(-1).expand(
            n_cells, self.n_genes, self.n_labels, B
        )  # (n, g, c, B)
        basis_means = self.basis_means.expand(
            n_cells, self.n_genes, self.n_labels, B
        )  # (n, g, c, B)
        phi = (  # (n, g, c)
            torch.sum(a * torch.exp(-b * torch.square(mu_ngcb - basis_means)), 3)
            + LOWER_BOUND
        )

        # compute gamma
        nb_pdf = NegativeBinomial(mu=mu_ngc, theta=phi)
        # pdf = prob dist func jetay data input diye probability pawa jbe
        x_ = x.unsqueeze(-1).expand(n_cells, self.n_genes, self.n_labels) 
        #2D expression matrix k 3D banaise(cell type add kre), ekta cell er value 5 hle, oitake 5 5 5 5 5 banaise
        #NB a mu_ngc, phi, x_ 3 tai 3D  
        #f = open("X.txt",'a')                             
        #print('whatever', file=f)
        #print("=================x==================", file=f)
        #print(x.shape, file=f)
        #print(x, file=f)
        
        #print("=================x_==================", file=f)
        #print(x_.shape, file=f)
        #print(x_, file=f)

        
        #f.close()
        
        x_log_prob_raw = nb_pdf.log_prob(x_)  # (n, g, c)
        # print("x_log_prob_raw:")
        # print(x_log_prob_raw)
        # Log likelihood  = log_prob 
        # x_log_prob_raw = log(NB)            =====>
        theta_log = theta_log.expand(n_cells, self.n_labels)
        p_x_c = torch.sum(x_log_prob_raw, 1) + theta_log  # (n, c)
        
        # sum er 2nd param: dim (int or tuple of python:ints) – the dimension or dimensions to reduce. 
        # ngc; so 1 dim = g; so g er upor sum ; log sum = Gun  
 
        normalizer_over_c = torch.logsumexp(p_x_c, 1)
        normalizer_over_c = normalizer_over_c.unsqueeze(-1).expand(
            n_cells, self.n_labels
        )
        gamma = torch.exp(p_x_c - normalizer_over_c)  # (n, c)
        # e^(a-b)  = e^a/ e^b; e^(lnx) = x 
        # p_x_c = ΠgN B μngc; ϕ~ngc  = gamma er uporer part 
        # normalizer_over_c = gamma er nicher part 
        #f = open("mu_ngc.txt",'a')
        #print('whatever', file=f)
        #print("=================mu_ngc==================", file=f)
        #print(mu_ngc.shape, file=f)
        #print(mu_ngc, file=f)
        
        #print("=================phi==================", file=f)
        #print(phi.shape, file=f)
        #print(phi, file=f)

        
        #f.close()
        

        return dict(
            mu=mu_ngc,
            phi=phi,
            gamma=gamma,
            p_x_c=p_x_c,
            s=size_factor,
        )
        
    @auto_move_data
    def prediction_generative(self, x, size_factor, design_matrix=None):
        real_marker_gene_mat = pd.read_csv('data/FL_celltype_all1.csv', index_col=0)
        real_rho = torch.Tensor(real_marker_gene_mat.to_numpy())
        # x has shape (n, g)
        delta = torch.exp(self.delta_log)  # (g, c)
        theta_log = F.log_softmax(self.theta_logit, dim=-1)  # (c)

        # compute mean of NegBin - shape (n_cells, n_genes, n_labels)
        n_cells = size_factor.shape[0]
        base_mean = torch.log(size_factor)  # (n, 1)
        base_mean = base_mean.unsqueeze(-1).expand(
            n_cells, self.n_genes, self.n_labels
        )  # (n, g, c)

        # compute beta (covariate coefficent)
        # design_matrix has shape (n,p)
        if design_matrix is not None:
            covariates = torch.einsum("np,gp->gn", design_matrix, self.beta)  # (g, n)
            covariates = torch.transpose(covariates, 0, 1).unsqueeze(-1)  # (n, g, 1)
            covariates = covariates.expand(n_cells, self.n_genes, self.n_labels)
            base_mean = base_mean + covariates

        # base gene expression
        b_g_0 = self.b_g_0.unsqueeze(-1).expand(n_cells, self.n_genes, self.n_labels)
        delta_rho = delta * real_rho #delta = parameter, rho = marker gene matrix er 0, 1 values
        delta_rho = delta_rho.expand(n_cells, self.n_genes, self.n_labels)  # (n, g, c)
        log_mu_ngc = base_mean + delta_rho + b_g_0
        mu_ngc = torch.exp(log_mu_ngc)  # (n, g, c)

        # compute phi of NegBin - shape (n_cells, n_genes, n_labels)
        a = torch.exp(self.log_a)  # (B)
        a = a.expand(n_cells, self.n_genes, self.n_labels, B)
        b_init = 2 * ((self.basis_means[1] - self.basis_means[0]) ** 2)
        b = torch.exp(torch.ones(B, device=x.device) * (-torch.log(b_init)))  # (B)
        b = b.expand(n_cells, self.n_genes, self.n_labels, B)
        mu_ngcb = mu_ngc.unsqueeze(-1).expand(
            n_cells, self.n_genes, self.n_labels, B
        )  # (n, g, c, B)
        basis_means = self.basis_means.expand(
            n_cells, self.n_genes, self.n_labels, B
        )  # (n, g, c, B)
        phi = (  # (n, g, c)
            torch.sum(a * torch.exp(-b * torch.square(mu_ngcb - basis_means)), 3)
            + LOWER_BOUND
        )

        # compute gamma
        nb_pdf = NegativeBinomial(mu=mu_ngc, theta=phi)
        # pdf = prob dist func jetay data input diye probability pawa jbe
        x_ = x.unsqueeze(-1).expand(n_cells, self.n_genes, self.n_labels)
        #print("x_")
        #print(x_.shape)
        
        x_log_prob_raw = nb_pdf.log_prob(x_)  # (n, g, c)
        # print("x_log_prob_raw:")
        # print(x_log_prob_raw)
        # Log likelihood  = log_prob 
        # x_log_prob_raw = log(NB)            =====>
        theta_log = theta_log.expand(n_cells, self.n_labels)
        p_x_c = torch.sum(x_log_prob_raw, 1) + theta_log  # (n, c)
        
        # sum er 2nd param: dim (int or tuple of python:ints) – the dimension or dimensions to reduce. 
        # ngc; so 1 dim = g; so g er upor sum ; log sum = Gun  
 
        normalizer_over_c = torch.logsumexp(p_x_c, 1)
        normalizer_over_c = normalizer_over_c.unsqueeze(-1).expand(
            n_cells, self.n_labels
        )
        gamma = torch.exp(p_x_c - normalizer_over_c)  # (n, c)
        # e^(a-b)  = e^a/ e^b; e^(lnx) = x 
        # p_x_c =ln( ΠgN B μngc ); ϕ~ngc  = gamma er uporer part er ln 
        # normalizer_over_c = gamma er nicher part er ln

        

        return dict(
            mu=mu_ngc,
            phi=phi,
            gamma=gamma,
            p_x_c=p_x_c,
            s=size_factor,
        )
        
    @auto_move_data
    def generative_toggle(self, x, size_factor, design_matrix=None):
        real_marker_gene_mat = pd.read_csv('data/FL_celltype_toggle.csv', index_col=0)
        real_rho = torch.Tensor(real_marker_gene_mat.to_numpy())
        # x has shape (n, g)
        delta = torch.exp(self.delta_log)  # (g, c)
        theta_log = F.log_softmax(self.theta_logit, dim=-1)  # (c)

        # compute mean of NegBin - shape (n_cells, n_genes, n_labels)
        n_cells = size_factor.shape[0]
        base_mean = torch.log(size_factor)  # (n, 1)
        base_mean = base_mean.unsqueeze(-1).expand(
            n_cells, self.n_genes, self.n_labels
        )  # (n, g, c)

        # compute beta (covariate coefficent)
        # design_matrix has shape (n,p)
        if design_matrix is not None:
            covariates = torch.einsum("np,gp->gn", design_matrix, self.beta)  # (g, n)
            covariates = torch.transpose(covariates, 0, 1).unsqueeze(-1)  # (n, g, 1)
            covariates = covariates.expand(n_cells, self.n_genes, self.n_labels)
            base_mean = base_mean + covariates

        # base gene expression
        b_g_0 = self.b_g_0.unsqueeze(-1).expand(n_cells, self.n_genes, self.n_labels)
        delta_rho = delta * real_rho #delta = parameter, rho = marker gene matrix er 0, 1 values
        delta_rho = delta_rho.expand(n_cells, self.n_genes, self.n_labels)  # (n, g, c)
        log_mu_ngc = base_mean + delta_rho + b_g_0
        mu_ngc = torch.exp(log_mu_ngc)  # (n, g, c)

        # compute phi of NegBin - shape (n_cells, n_genes, n_labels)
        a = torch.exp(self.log_a)  # (B)
        a = a.expand(n_cells, self.n_genes, self.n_labels, B)
        b_init = 2 * ((self.basis_means[1] - self.basis_means[0]) ** 2)
        b = torch.exp(torch.ones(B, device=x.device) * (-torch.log(b_init)))  # (B)
        b = b.expand(n_cells, self.n_genes, self.n_labels, B)
        mu_ngcb = mu_ngc.unsqueeze(-1).expand(
            n_cells, self.n_genes, self.n_labels, B
        )  # (n, g, c, B)
        basis_means = self.basis_means.expand(
            n_cells, self.n_genes, self.n_labels, B
        )  # (n, g, c, B)
        phi = (  # (n, g, c)
            torch.sum(a * torch.exp(-b * torch.square(mu_ngcb - basis_means)), 3)
            + LOWER_BOUND
        )

        # compute gamma
        nb_pdf = NegativeBinomial(mu=mu_ngc, theta=phi)
        # pdf = prob dist func jetay data input diye probability pawa jbe
        x_ = x.unsqueeze(-1).expand(n_cells, self.n_genes, self.n_labels)
        #print("x_")
        #print(x_.shape)
        
        x_log_prob_raw = nb_pdf.log_prob(x_)  # (n, g, c)
        # print("x_log_prob_raw:")
        # print(x_log_prob_raw)
        # Log likelihood  = log_prob 
        # x_log_prob_raw = log(NB)            =====>
        theta_log = theta_log.expand(n_cells, self.n_labels)
        p_x_c = torch.sum(x_log_prob_raw, 1) + theta_log  # (n, c)
        
        # sum er 2nd param: dim (int or tuple of python:ints) – the dimension or dimensions to reduce. 
        # ngc; so 1 dim = g; so g er upor sum ; log sum = Gun  
 
        normalizer_over_c = torch.logsumexp(p_x_c, 1)
        normalizer_over_c = normalizer_over_c.unsqueeze(-1).expand(
            n_cells, self.n_labels
        )
        gamma = torch.exp(p_x_c - normalizer_over_c)  # (n, c)
        # e^(a-b)  = e^a/ e^b; e^(lnx) = x 
        # p_x_c =ln( ΠgN B μngc ); ϕ~ngc  = gamma er uporer part er ln 
        # normalizer_over_c = gamma er nicher part er ln

        

        return dict(
            mu=mu_ngc,
            phi=phi,
            gamma=gamma,
            p_x_c=p_x_c,
            s=size_factor,
        )

    def loss(
        self,
        tensors,
        inference_outputs,
        generative_outputs,
        n_obs: int = 1.0,
    ):
        # generative_outputs is a dict of the return value from `generative(...)`
        # assume that `n_obs` is the number of training data points
        #generative_inputs = self._get_generative_input(tensors, None)
        #outputs = self.module.generative(**generative_inputs) #original
        #outputs = self.prediction_generative(**generative_inputs) #all 1
        #gamma = outputs["gamma"]
        #p_x_c = outputs["p_x_c"]
        
        
        p_x_c = generative_outputs["p_x_c"]
        gamma = generative_outputs["gamma"]

        # compute Q
        # take mean of number of cells and multiply by n_obs (instead of summing n)
        q_per_cell = torch.sum(gamma * -p_x_c, 1)
        #print("Q=======================")
        #print(q_per_cell)
        #print(q_per_cell.shape) #1024

        # third term is log prob of prior terms in Q
        theta_log = F.log_softmax(self.theta_logit, dim=-1)
        theta_log_prior = Dirichlet(self.dirichlet_concentration)
        theta_log_prob = -theta_log_prior.log_prob(
            torch.exp(theta_log) + THETA_LOWER_BOUND
        )
        prior_log_prob = theta_log_prob
        delta_log_prior = Normal(
            self.delta_log_mean, self.delta_log_log_scale.exp().sqrt()
        )
        delta_log_prob = torch.masked_select(
            delta_log_prior.log_prob(self.delta_log), (self.rho > 0)
        )
        prior_log_prob += -torch.sum(delta_log_prob)
        #print("n obss")
        #print(n_obs)
        #print(type(n_obs))
        #M = 1024 (minibatch size)
        #N = 9956 (no of cells)
        # loss = Q/n_obs
        loss = (torch.mean(q_per_cell) * n_obs + prior_log_prob) / n_obs

        return LossRecorder(
            loss, q_per_cell, torch.zeros_like(q_per_cell), prior_log_prob
        )

    @torch.no_grad()
    def sample(
        self,
        tensors,
        n_samples=1,
        library_size=1,
    ):
        raise NotImplementedError("No sampling method for CellAssign")
