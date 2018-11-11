import numpy as np
import scipy as sp
import scipy.sparse
import math
import sys
sys.path.insert(0, '../..')

from bayesbridge import BayesBridge

data_folder = 'saved_outputs/'
test_combo = [
    ('linear', 'cg', 'dense', False),
    ('logit', 'direct', 'dense', False),
    ('logit', 'direct', 'dense', True),
    ('logit', 'cg', 'sparse', False)
]

def test_gibbs():

    for model, sampling_method, matrix_format, restart_im_middle in test_combo:
        samples = run_gibbs(model, sampling_method, matrix_format, restart_im_middle)
        assert is_same_as_prev_output(samples, sampling_method, model)

def run_gibbs(model, sampling_method, matrix_format, restart_in_middle=False):

    n_burnin = 0
    n_post_burnin = 10
    thin = 1
    reg_exponent = 0.5

    y, X = simulate_data(model, matrix_format)
    bridge = BayesBridge(y, X, model=model)
    init = {
        'tau': 1,
        'lambda': np.ones(X.shape[1])
    }

    if restart_in_middle:
        n_total_post_burnin = n_post_burnin
        n_post_burnin = math.ceil(n_total_post_burnin / 2)

    mcmc_output = bridge.gibbs(
        n_burnin, n_post_burnin, thin, reg_exponent, init,
        sampling_method=sampling_method, seed=0, params_to_save='all'
    )

    if restart_in_middle:
        reinit_bridge = BayesBridge(y, X, model=model)
        mcmc_output = reinit_bridge.gibbs_additional_iter(
            mcmc_output, n_total_post_burnin - n_post_burnin, merge=True
        )

    return mcmc_output['samples']

def simulate_data(model, matrix_format):

    np.random.seed(1)
    n = 500
    p = 500

    # True parameters
    sigma_true = 2
    beta_true = np.zeros(p)
    beta_true[:5] = 4
    beta_true[5:15] = 2 ** - np.linspace(0.0, 4.5, 10)

    X = np.random.randn(n, p)
    if model == 'linear':
        y = np.dot(X, beta_true) + sigma_true * np.random.randn(n)
    elif model == 'logit':
        mu = (1 + np.exp(- np.dot(X, beta_true))) ** -1
        y = (np.random.binomial(1, mu), None)
    else:
        raise NotImplementedError()

    if matrix_format == 'sparse':
        X = sp.sparse.csr_matrix(X)

    return y, X

def load_data(sampling_method, model):
    return np.load(get_filename(sampling_method, model))

def get_filename(sampling_method, model):
    return data_folder + '_'.join([
        model, sampling_method, 'samples.npy'
    ])

def save_data(samples, sampling_method, model):
    np.save(get_filename(sampling_method, model), samples['beta'])

def is_same_as_prev_output(samples, sampling_method, model):
    prev_sample = load_data(sampling_method, model)
    return np.allclose(samples['beta'], prev_sample, rtol=.001, atol=10e-6)


if __name__ == '__main__':
    update_output = False
    if update_output:
        for model, sampling_method, matrix_format, restart_im_middle in test_combo:
            samples = run_gibbs(model, sampling_method, matrix_format, restart_im_middle)
            save_data(samples, sampling_method, model)