import math
from math import sqrt, log, pow
from .custom_math import exp, sinc
import random

class ExpTiltedStableDist():

    def __init__(self, seed=None):
        random.seed(seed)
        self.unif_rv = random.random
        self.normal_rv = random.normalvariate

    def rv(self, alpha, lam):
        """
        Generate a random variable from a stable distribution with
            characteristic exponent =  alpha < 1
            skewness = 1
            scale = cos(alpha * pi / 2) ** (1 / alpha)
            location = 0
            exponential tilting = lam
        (The density p(x) is tilted by exp(-lam * x).)
        """

        b = (1. - alpha) / alpha
        lam_alpha = pow(lam, alpha)
        gamma = lam_alpha * alpha * (1. - alpha)
        sqrt_gamma = sqrt(gamma)
        c1 = sqrt(math.pi / 2.)
        c2 = 2. + c1
        c3 = c2 * sqrt_gamma
        xi = (1. + sqrt(2.) * c3) / math.pi
        psi = c3 * exp(-gamma * math.pi * math.pi / 8.) / sqrt(math.pi)

        accepted = False
        while not accepted:
            U, Z, z = self.sample_aux_rv(c1, xi, psi, gamma, sqrt_gamma, alpha, lam_alpha)
            X, N, E, a, m, delta = \
                self.sample_reference_rv(U, alpha, lam_alpha, b, c1, z)
            log_accept_prob = \
                self.compute_log_accept_prob(X, N, E, a, m, alpha, lam_alpha, b, delta)
            accepted = (log_accept_prob > log(Z))

        return pow(X, -b)

    def rv_hofert(self, alpha, lam):

        X = 0.
        m = max(1, math.floor(pow(lam, alpha)))
        c = pow(1. / m, 1. / alpha)
        for i in range(m):
            X += self.sample_divided_rv(alpha, lam, c)

        return X

    def sample_divided_rv(self, alpha, lam, c):
        accepted = False
        while not accepted:
            S = c * self.sample_non_tilted_rv(alpha)
            accept_prob = exp(- lam * S)
            accepted = (self.unif_rv() < accept_prob)
        return S

    def sample_non_tilted_rv(self, alpha):
        V = self.unif_rv()
        E = - log(self.unif_rv())
        S = pow(
            self.zolotarev_function(math.pi * V, alpha) / E
        , (1. - alpha) / alpha)
        return S

    def sample_aux_rv(self, c1, xi, psi, gamma, sqrt_gamma, alpha, lam_alpha):
        """
        Samples an auxiliary random variable for the double-rejection algorithm.
        Returns:
            U : auxiliary random variable for the double-rejection algorithm
            Z : uniform random variable independent of U, X
            z : scalar quantity used later
        """

        accepted = False
        while not accepted:
            U = self.sample_aux2_rv(c1, xi, psi, gamma, sqrt_gamma)
            zeta = sqrt(self.zolotarev_pdf_exponentiated(U, alpha))
            z = 1. / (1. - pow(1. + alpha * zeta / sqrt_gamma, -1. / alpha))
            accept_prob = self.compute_aux2_accept_prob(
                U, c1, xi, psi, zeta, z, lam_alpha, gamma, sqrt_gamma)
            if accept_prob == 0.:
                accepted = False
            else:
                Z = self.unif_rv() / accept_prob
                accepted = (U < math.pi and Z <= 1.)

        return U, Z, z

    def sample_aux2_rv(self, c1, xi, psi, gamma, sqrt_gamma):
        """
        Sample the 2nd level auxiliary random variable (i.e. the additional
        auxiliary random variable used to sample the auxilary variable for
        double-rejection algorithm.)
        """

        w1 = c1 * xi / sqrt_gamma
        w2 = 2. * sqrt(math.pi) * psi
        w3 = xi * math.pi
        V = self.unif_rv()
        if gamma >= 1:
            if V < w1 / (w1 + w2):
                U = abs(self.normal_rv(0., 1.)) / sqrt_gamma
            else:
                W = self.unif_rv()
                U = math.pi * (1. - W * W)
        else:
            W = self.unif_rv()
            if V < w3 / (w2 + w3):
                U = math.pi * W
            else:
                U = math.pi * (1. - W * W)

        return U

    def compute_aux2_accept_prob(self, U, c1, xi, psi, zeta, z, lam_alpha, gamma, sqrt_gamma):
        inverse_accept_prob = math.pi * exp(-lam_alpha * (1. - 1. / (zeta * zeta))) \
              / ((1. + c1) * sqrt_gamma / zeta + z)
        d = 0.
        if U >= 0. and gamma >= 1:
            d += xi * exp(-gamma * U * U / 2.)
        if U > 0. and U < math.pi:
            d += psi / sqrt(math.pi - U)
        if U >= 0. and U <= math.pi and gamma < 1.:
            d += xi
        inverse_accept_prob *= d
        accept_prob = 1 / inverse_accept_prob
        return accept_prob

    def sample_reference_rv(self, U, alpha, lam_alpha, b, c1, z):
        """
        Generate a sample from the reference (augmented) distribution conditional
        on U for the double-rejection algorithm

        Returns:
        --------
            X : random variable from the reference distribution
            N, E : random variables used later for computing the acceptance prob
            a, m, delta: scalar quantities used later
        """
        a = self.zolotarev_function(U, alpha)
        m = pow(b / a, alpha) * lam_alpha
        delta = sqrt(m * alpha / a)
        a1 = delta * c1
        a3 = z / a
        s = a1 + delta + a3
        V2 = self.unif_rv()
        N = 0.
        E = 0.
        if V2 < a1 / s:
            N = self.normal_rv(0., 1.)
            X = m - delta * abs(N)
        elif V2 < (a1 + delta) / s:
            X = m + delta * self.unif_rv()
        else:
            E = - log(self.unif_rv())
            X = m + delta + E * a3
        return X, N, E, a, m, delta

    def compute_log_accept_prob(self, X, N, E, a, m, alpha, lam_alpha, b, delta):

        if X < 0:
            log_accept_prob = - math.inf
        else:
            log_accept_prob = - (
                a * (X - m)
                + exp((1. / alpha) * log(lam_alpha) - b * log(m)) * (pow(m / X, b) - 1.)
            )
            if X < m:
                log_accept_prob += N * N / 2.
            elif X > m + delta:
                log_accept_prob += E
                
        return log_accept_prob

    def zolotarev_pdf_exponentiated(self, x, alpha):
        """
        Evaluates a function proportional to a power of the Zolotarev density.
        """
        denominator = pow(sinc(alpha * x), alpha) \
                      * pow(sinc((1. - alpha) * x), (1. - alpha))
        numerator = sinc(x)
        return numerator / denominator

    def zolotarev_function(self, x, alpha):
        val = pow(
            pow((1. - alpha) * sinc((1. - alpha) * x), (1. - alpha))
            * pow(alpha * sinc(alpha * x), alpha)
            / sinc(x)
        , 1. / (1. - alpha))
        return val