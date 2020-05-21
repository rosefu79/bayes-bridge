import numpy as np
import scipy as sp
import scipy.linalg
import scipy.sparse
import math
import time
import pdb
from .util.simple_warnings import warn_message_only
from .random import BasicRandom
from .reg_coef_sampler import SparseRegressionCoefficientSampler
from .design_matrix import SparseDesignMatrix, DenseDesignMatrix
from .model import LinearModel, LogisticModel, CoxModel
from .prior import RegressionCoefPrior
from .chain_manager import MarkovChainManager


class BayesBridge():

    def __init__(self, outcome, X, model='linear', add_intercept=None,
                 center_predictor=False, prior=None):
        """
        Params
        ------
        outcome : vector if model == 'linear' else tuple
            (n_success, n_trial) if model == 'logistic'.
                The outcome is assumed binary if n_trial is None.
            (event_time, censoring_time) if model == 'cox'
        X : numpy array or scipy sparse matrix
        n_trial : vector
            Used for the logistic model for binomial outcomes.
        model : str, {'linear', 'logit'}
        """

        # TODO: Make each MCMC run more "independent" i.e. not rely on the
        # previous instantiation of the class. The initial run of the Gibbs
        # sampler probably depends too much the stuffs here.

        if add_intercept is None:
            add_intercept = (model != 'cox')

        if prior is None:
            prior = RegressionCoefPrior()

        if model == 'cox':
            if add_intercept:
                add_intercept = False
                warn_message_only(
                    "Intercept is not identifiable in the Cox model and will "
                    "not be added.")
            event_time, censoring_time = outcome
            event_time, censoring_time, X = CoxModel.preprocess_data(
                event_time, censoring_time, X
            )

        self.n_unshrunk = prior.n_fixed
        self.prior_sd_for_unshrunk = prior.sd_for_fixed.copy()
        if add_intercept:
            self.n_unshrunk += 1
            self.prior_sd_for_unshrunk = np.concatenate((
                [prior.sd_for_intercept], self.prior_sd_for_unshrunk
            ))

        DesignMatrix = SparseDesignMatrix \
            if sp.sparse.issparse(X) else DenseDesignMatrix
        X = DesignMatrix(
            X, add_intercept=add_intercept, center_predictor=center_predictor)

        if model == 'linear':
            self.model = LinearModel(outcome, X)
        elif model == 'logit':
            n_success, n_trial = outcome
            self.model = LogisticModel(n_success, X, n_trial)
        elif model == 'cox':
            self.model = CoxModel(event_time, censoring_time, X)
        else:
            raise NotImplementedError()

        self.n_obs = X.shape[0]
        self.n_pred = X.shape[1]
        self.prior = prior
        self.rg = BasicRandom()
        self.manager = MarkovChainManager(
            self.n_obs, self.n_pred, self.n_unshrunk, model
        )

    # TODO: write a test to ensure that the output when resuming the Gibbs
    # sampler coincide with that without interruption.
    def gibbs_additional_iter(
            self, mcmc_output, n_iter, n_status_update=0,
            merge=False, deallocate=False):
        """
        Continue running the Gibbs sampler from the previous state.

        Parameter
        ---------
        mcmc_output : the output of the 'gibbs' method.
        """

        if merge and deallocate:
            warn_message_only(
                "To merge the outputs, the previous one cannot be deallocated.")
            deallocate = False

        self.rg.set_state(mcmc_output['_random_gen_state'])

        init = mcmc_output['_markov_chain_state']
        thin, bridge_exp, sampling_method, global_scale_update = (
            mcmc_output[key] for key in [
                'thin', 'bridge_exponent', 'sampling_method', 'global_scale_update'
            ]
        )
        params_to_save = mcmc_output['samples'].keys()

        # Initalize the regression coefficient sampler with the previous state.
        self.reg_coef_sampler = SparseRegressionCoefficientSampler(
            self.n_pred, self.prior_sd_for_unshrunk, sampling_method
        )
        self.reg_coef_sampler.set_internal_state(mcmc_output['_reg_coef_sampler_state'])

        if deallocate:
            mcmc_output.clear()

        next_mcmc_output = self.gibbs(
            0, n_iter, thin, bridge_exp, init,
            sampling_method=sampling_method,
            global_scale_update=global_scale_update,
            params_to_save=params_to_save,
            n_status_update=n_status_update,
            _add_iter_mode=True
        )
        if merge:
            next_mcmc_output \
                = self.manager.merge_outputs(mcmc_output, next_mcmc_output)

        return next_mcmc_output

    # TODO: Make dedicated functions for specifying 1) prior hyper-parameters,
    #  and 2) sampler tuning parameters (maybe).
    def gibbs(self, n_burnin, n_post_burnin, thin=1, bridge_exponent=.5,
              init={}, sampling_method='cg', seed=None,
              global_scale_update='sample', params_to_save=None,
              n_init_optim_step=10, n_status_update=0, _add_iter_mode=False,
              hmc_curvature_est_stabilized=False):
        """
        MCMC implementation for the Bayesian bridge.

        Parameters
        ----------
        n_burnin : int
            number of burn-in samples to be discarded
        n_post_burnin : int
            number of posterior draws to be saved
        sampling_method : str, {'direct', 'cg', 'hmc'}
        global_scale_update : str, {'sample', 'optimize', None}
        params_to_save : {None, 'all', list of str}
        n_init_optim_step : int
            If > 0, the Markov chain will be run after the specified number of
            optimization steps in which the regression coefficients are
            optimized conditionally on the shrinkage parameters. During the
            optimization, the global shrinkage parameter is fixed while the
            local ones are sampled.
        n_status_update : int
            Number of updates to print on stdout during the sampler run.
        """

        n_iter = n_burnin + n_post_burnin

        if _add_iter_mode:
            n_init_optim_step = 0
        else:
            self.rg.set_seed(seed)
            self.reg_coef_sampler = SparseRegressionCoefficientSampler(
                self.n_pred, self.prior_sd_for_unshrunk, sampling_method,
                hmc_curvature_est_stabilized, self.prior.slab_size
            )

        if params_to_save == 'all':
            params_to_save = [
                'regress_coef', 'local_scale', 'global_scale', 'logp'
            ]
            if self.model != 'cox':
                params_to_save.append('obs_prec')
        elif params_to_save is None:
            params_to_save = ['regress_coef', 'global_scale', 'logp']

        n_status_update = min(n_iter, n_status_update)
        start_time = time.time()
        self.manager.stamp_time(start_time)

        # Initial state of the Markov chain
        coef, obs_prec, lscale, gscale, init, initial_optim_info = \
            self.initialize_chain(init, bridge_exponent, n_init_optim_step)
        if n_init_optim_step > 0:
            self.manager.print_status(
                n_status_update, 0, n_iter, msg_type='optim', time_format='second')

        # Pre-allocate
        samples = {}
        sampling_info = {}
        self.manager.pre_allocate(
            samples, sampling_info, n_post_burnin, thin, params_to_save, sampling_method
        )

        # Start Gibbs sampling
        for mcmc_iter in range(1, n_iter + 1):

            coef, info = self.update_regress_coef(
                coef, obs_prec, gscale, lscale, sampling_method
            )

            obs_prec = self.update_obs_precision(coef)

            # Draw from gscale | coef and then lscale | gscale, coef.
            # (The order matters.)
            gscale = self.update_global_scale(
                gscale, coef[self.n_unshrunk:], bridge_exponent,
                method=global_scale_update)

            lscale = self.update_local_scale(
                gscale, coef[self.n_unshrunk:], bridge_exponent)

            logp = self.compute_posterior_logprob(
                coef, gscale, obs_prec, bridge_exponent
            )

            self.manager.store_current_state(
                samples, mcmc_iter, n_burnin, thin, coef, lscale, gscale,
                obs_prec, logp, params_to_save
            )
            self.manager.store_sampling_info(
                sampling_info, info, mcmc_iter, n_burnin, thin, sampling_method
            )
            self.manager.print_status(n_status_update, mcmc_iter, n_iter)

        runtime = time.time() - start_time

        if self.prior.gscale_paramet == 'regress_coef':
            gscale, lscale, unit_bridge_magitude = \
                self.adjust_scale(gscale, lscale, bridge_exponent, to='regress_coef')
            if 'global_scale' in samples:
                samples['global_scale'] *= unit_bridge_magitude
            if 'local_scale' in samples:
                samples['local_scale'] /= unit_bridge_magitude

        _markov_chain_state = \
            self.manager.pack_parameters(coef, obs_prec, lscale, gscale)

        mcmc_output = {
            'samples': samples,
            'init': init,
            'n_burnin': n_burnin,
            'n_post_burnin': n_post_burnin,
            'thin': thin,
            'seed': seed,
            'n_coef_wo_shrinkage': self.n_unshrunk,
            'prior_sd_for_unshrunk': self.prior_sd_for_unshrunk,
            'bridge_exponent': bridge_exponent,
            'sampling_method': sampling_method,
            'runtime': runtime,
            'global_scale_update': global_scale_update,
            'initial_optimization_info': initial_optim_info,
            'reg_coef_sampling_info': sampling_info,
            '_markov_chain_state': _markov_chain_state,
            '_random_gen_state': self.rg.get_state(),
            '_reg_coef_sampler_state': self.reg_coef_sampler.get_internal_state()
        }

        return mcmc_output

    def initialize_chain(self, init, bridge_exp, n_optim):
        # Choose the user-specified state if provided, the default ones otherwise.

        if 'regress_coef' in init:
            coef = init['regress_coef']
            if not len(coef) == self.n_pred:
                raise ValueError('An invalid initial state.')
        else:
            coef = np.zeros(self.n_pred)
            if 'intercept' in init:
                coef[0] = init['intercept']

        if 'obs_prec' in init:
            obs_prec = np.ascontiguousarray(init['obs_prec'])
                # Cython requires a C-contiguous array.
            if not len(obs_prec) == self.n_obs:
                raise ValueError('An invalid initial state.')
        elif self.model.name == 'linear':
            obs_prec = np.mean((self.model.y - self.model.X.dot(coef)) ** 2) ** -1
        elif self.model.name == 'logit':
            obs_prec = LogisticModel.compute_polya_gamma_mean(
                self.model.n_trial, self.model.X.dot(coef)
            )
        else:
            obs_prec = None

        lscale, gscale = self.initialize_shrinkage_parameters(init, bridge_exp)

        info_keys = ['is_success', 'n_design_matvec', 'n_iter']
        optim_info = {
            key: np.zeros(n_optim, dtype=np.int) for key in info_keys
        }
        optim_info['n_optim'] = n_optim
        for i in range(n_optim):
            coef, info = self.reg_coef_sampler.search_mode(
                coef, lscale, gscale, obs_prec, self.model
            )
            for key in info_keys:
                optim_info[key][i] = info[key]
            obs_prec = self.update_obs_precision(coef)
            lscale = self.update_local_scale(
                gscale, coef[self.n_unshrunk:], bridge_exp
            )
            
        init = {
            'regress_coef': coef,
            'obs_prec': obs_prec,
            'local_scale': lscale,
            'global_scale': gscale
        }

        return coef, obs_prec, lscale, gscale, init, optim_info

    def initialize_shrinkage_parameters(self, init, bridge_exp):
        """
        Current options allow specifying 1) both scale parameters directly,
        2) regression coefficients only, and 3) global scale only.
        """
        gscale_default = .1
        if self.prior.gscale_paramet == 'raw':
            gscale_default \
                /= self.prior.compute_power_exp_ave_magnitude(bridge_exp)

        if 'local_scale' in init and 'global_scale' in init:
            lscale = init['local_scale']
            gscale = init['global_scale']
            if not len(lscale) == (self.n_pred - self.n_unshrunk):
                raise ValueError('An invalid initial state.')

        elif 'regress_coef' in init:
            gscale = self.update_global_scale(
                None, init['regress_coef'][self.n_unshrunk:], bridge_exp,
                method='optimize'
            )
            lscale = self.update_local_scale(
                gscale, init['regress_coef'][self.n_unshrunk:], bridge_exp
            )
        else:
            if 'global_scale' in init:
                gscale = init['global_scale']
            else:
                gscale = gscale_default
            lscale = np.ones(self.n_pred - self.n_unshrunk) / gscale

        if self.prior.gscale_paramet == 'regress_coef':
            # Gibbs sampler requires the raw parametrization. Technically only
            # gscale * lscale matters within the sampler due to the update order.
            gscale, lscale, unit_bridge_magnitude \
                = self.adjust_scale(gscale, lscale, bridge_exp, to='raw')

        return lscale, gscale

    def adjust_scale(self, gscale, lscale, bridge_exp, to):
        unit_bridge_magnitude \
            = self.prior.compute_power_exp_ave_magnitude(bridge_exp, 1.)
        if to == 'raw':
            gscale /= unit_bridge_magnitude
            lscale *= unit_bridge_magnitude
        elif to == 'regress_coef':
            gscale *= unit_bridge_magnitude
            lscale /= unit_bridge_magnitude
        else:
            raise ValueError()
        return gscale, lscale, unit_bridge_magnitude

    def update_regress_coef(self, coef, obs_prec, gscale, lscale, sampling_method):

        if sampling_method in ('direct', 'cg'):

            if self.model.name == 'linear':
                y_gaussian = self.model.y
                obs_prec = obs_prec * np.ones(self.n_obs)
            elif self.model.name == 'logit':
                y_gaussian = (self.model.n_success - self.model.n_trial / 2) / obs_prec

            coef, info = self.reg_coef_sampler.sample_gaussian_posterior(
                y_gaussian, self.model.X, obs_prec, gscale, lscale,
                sampling_method
            )

        elif sampling_method in ['hmc', 'nuts']:
            coef, info = self.reg_coef_sampler.sample_by_hmc(
                coef, gscale, lscale, self.model, method=sampling_method
            )

        else:
            raise NotImplementedError()

        return coef, info

    def update_obs_precision(self, coef):

        obs_prec = None
        if self.model.name == 'linear':
            resid = self.model.y - self.model.X.dot(coef)
            scale = np.sum(resid ** 2) / 2
            obs_var = scale / self.rg.np_random.gamma(self.n_obs / 2, 1)
            obs_prec = 1 / obs_var
        elif self.model.name == 'logit':
            obs_prec = self.rg.polya_gamma(
                self.model.n_trial.astype(np.intc), self.model.X.dot(coef)
            )

        return obs_prec

    def update_global_scale(
            self, gscale, beta_with_shrinkage, bridge_exp,
            coef_expected_magnitude_lower_bd=.001, method='sample'):
        # :param method: {"sample", "optimize", None}

        if beta_with_shrinkage.size == 0:
            return 1. # arbitrary float value as a placeholder

        lower_bd = coef_expected_magnitude_lower_bd \
                   / self.prior.compute_power_exp_ave_magnitude(bridge_exp)
            # Solve for the value of global shrinkage such that
            # (expected value of regress_coef given gscale) = coef_expected_magnitude_lower_bd.

        if method == 'optimize':
            gscale = self.monte_carlo_em_global_scale(
                beta_with_shrinkage, bridge_exp)

        elif method == 'sample':
            # Conjugate update for phi = 1 / gscale ** bridge_exp
            if np.count_nonzero(beta_with_shrinkage) == 0:
                gscale = 0
            else:
                prior_param = self.prior.param['gscale_neg_power']
                shape, rate = prior_param['shape'], prior_param['rate']
                shape += beta_with_shrinkage.size / bridge_exp
                rate += np.sum(np.abs(beta_with_shrinkage) ** bridge_exp)
                phi = self.rg.np_random.gamma(shape, scale=1 / rate)
                gscale = 1 / phi ** (1 / bridge_exp)

        if (method is not None) and gscale < lower_bd:
            gscale = lower_bd
            warn_message_only(
                "The global shrinkage parameter update returned an unreasonably "
                "small value. Returning a specified lower bound value instead."
            )

        return gscale

    def monte_carlo_em_global_scale(
            self, beta_with_shrinkage, bridge_exp):
        """ Maximize the likelihood (not posterior conditional) 'coef | gscale'. """
        phi = len(beta_with_shrinkage) / bridge_exp \
              / np.sum(np.abs(beta_with_shrinkage) ** bridge_exp)
        gscale = phi ** - (1 / bridge_exp)
        return gscale

    def update_local_scale(self, gscale, beta_with_shrinkage, bridge_exp):

        lscale_sq = .5 / self.rg.tilted_stable(
            bridge_exp / 2, (beta_with_shrinkage / gscale) ** 2
        )
        lscale = np.sqrt(lscale_sq)

        # TODO: Pick the lower and upper bound more carefully.
        if np.any(lscale == 0):
            warn_message_only(
                "Local scale parameter under-flowed. Replacing with a small number.")
            lscale[lscale == 0] = 10e-16
        elif np.any(np.isinf(lscale)):
            warn_message_only(
                "Local scale parameter over-flowed. Replacing with a large number.")
            lscale[np.isinf(lscale)] = 2.0 / gscale

        return lscale

    def compute_posterior_logprob(self, coef, gscale, obs_prec, bridge_exp):

        # Contributions from the likelihood.
        params = [coef] if self.model.name != 'linear' else [coef, obs_prec]
        loglik, _ = self.model.compute_loglik_and_gradient(*params, loglik_only=True)

        # Contributions from the regularization.
        loglik += - .5 * np.sum((coef / self.prior.slab_size) ** 2)

        # Add contributions from the priors.
        prior_logp = 0
        n_shrunk_coef = len(coef) - self.n_unshrunk

        # for coef | gscale.
        prior_logp += \
            - n_shrunk_coef * math.log(gscale) \
            - np.sum(np.abs(coef[self.n_unshrunk:] / gscale) ** bridge_exp)

        # for coefficients without shrinkage.
        prior_logp += - 1 / 2 * np.sum(
            (coef[:self.n_unshrunk] / self.prior_sd_for_unshrunk) ** 2
        )
        prior_logp += - np.sum(np.log(
            self.prior_sd_for_unshrunk[self.prior_sd_for_unshrunk < float('inf')]
        ))
        prior_param = self.prior.param['gscale_neg_power']
        prior_logp += (prior_param['shape'] - 1.) * math.log(gscale) \
                      - prior_param['rate'] * gscale

        logp = loglik + prior_logp

        return logp
