import warnings
from collections import defaultdict

import chainer
from chainer import configuration
import chainer.functions as F
import chainer.links as L
from chainer import reporter

import numpy

import sparse_chainer
import vd_functions as VDF

# Memo: If p=0.95, then alpha=19. ln(19) = 2.94443897917.
#       Thus, log_alpha_threashold is set 3.0 approximately.

configuration.config.user_memory_efficiency = 0
# 1<= : simple calculation like an element-wise one
# 2<= : little simple calculation like a series of element-wise ones
# 3<= : complex calculations like matrix ones
# more memory efficient, it takes much time

P_THRESHOLD = 0.95
LOGA_THRESHOLD = 3.
INITIAL_LOG_SIGMA2 = chainer.initializers.Constant(-10.)


def get_vd_links(link):
    if isinstance(link, chainer.Chain):
        for child_link in link.links(skipself=True):
            for vd_child_link in get_vd_links(child_link):
                yield vd_child_link
    else:
        if getattr(link, 'is_variational_dropout', False):
            yield link


def calculate_p(link):
    """Calculate (something like) probabilities of variational dropout
    This method takes high computational cost.
    """
    alpha = link.xp.exp(VDF.calculate_log_alpha(
        link.W, link.log_sigma2, eps=1e-8, thresholds=(-8., 8.)).data)
    p = alpha / (1 + alpha)
    return p


def calculate_stats(chain, threshold=P_THRESHOLD):
    """Calculate stats for parameters of variational dropout
    This method takes high computational cost.
    """
    xp = chain.xp
    stats = {}
    all_p = [calculate_p(link).flatten()
             for link in get_vd_links(chain)]
    if not all_p:
        return defaultdict(float)
    all_p = xp.concatenate(all_p, axis=0)
    stats['mean_p'] = xp.mean(all_p)

    all_threshold = [link.p_threshold
                     for link in get_vd_links(chain)]
    if any(th != threshold for th in all_threshold):
        warnings.warn('The threshold for sparsity calculation'
                      ' is different from'
                      ' thresholds used for prediction with'
                      ' threshold-based pruning.')
    # TODO: directly use threshold of each link

    is_zero = (all_p > threshold)
    stats['sparsity'] = xp.mean(is_zero)

    n_non_zero = (1 - is_zero).sum()
    if n_non_zero == 0:
        stats['W/Wnz'] = float('inf')
    else:
        stats['W/Wnz'] = all_p.size * 1. / n_non_zero
    return stats


class VariationalDropoutLinear(chainer.links.Linear):

    def __init__(self, in_size, out_size, nobias=False,
                 initialW=None, initial_bias=None,
                 p_threshold=P_THRESHOLD, loga_threshold=LOGA_THRESHOLD,
                 initial_log_sigma2=INITIAL_LOG_SIGMA2):
        super(VariationalDropoutLinear, self).__init__(
            in_size, out_size, nobias=nobias,
            initialW=initialW, initial_bias=initial_bias)
        self.add_param('log_sigma2', initializer=initial_log_sigma2)
        if in_size is not None:
            self._initialize_params(in_size, log_sigma2=True)
        self.p_threshold = p_threshold
        self.loga_threshold = loga_threshold
        self.is_variational_dropout = True
        self.is_variational_dropout_linear = True

    def _initialize_params(self, in_size, log_sigma2=False):
        if not log_sigma2:
            self.W.initialize((self.out_size, in_size))
        else:
            self.log_sigma2.initialize((self.out_size, in_size))

    def get_sparse_cpu_model(self):
        log_alpha = VDF.calculate_log_alpha(
            self.W, self.log_sigma2, eps=1e-8, thresholds=(-8., 8.))
        clip_mask = (log_alpha.data > self.loga_threshold)
        return sparse_chainer.SparseLinearForwardCPU(self, (1. - clip_mask))

    def __call__(self, x):
        if self.W.data is None:
            self._initialize_params(x.size // x.shape[0])

        return VDF.vd_linear(
            x, self.W, self.b, self.loga_threshold, log_sigma2=self.log_sigma2,
            log_alpha=None, eps=1e-8, thresholds=(-8., 8.))


def _pair(x):
    if hasattr(x, '__getitem__'):
        return x
    return x, x


class VariationalDropoutConvolution2D(chainer.links.Convolution2D):

    def __init__(self, in_channels, out_channels, ksize, stride=1, pad=0,
                 nobias=False, initialW=None, initial_bias=None,
                 p_threshold=P_THRESHOLD, loga_threshold=LOGA_THRESHOLD,
                 initial_log_sigma2=INITIAL_LOG_SIGMA2):
        super(VariationalDropoutConvolution2D, self).__init__(
            in_channels, out_channels, ksize, stride, pad,
            nobias=nobias, initialW=initialW,
            initial_bias=initial_bias)

        self.add_param('log_sigma2', initializer=initial_log_sigma2)
        if in_channels is not None:
            self._initialize_params(in_channels, log_sigma2=True)
        self.p_threshold = p_threshold
        self.loga_threshold = loga_threshold
        self.is_variational_dropout = True

    def _initialize_params(self, in_channels, log_sigma2=False):
        kh, kw = _pair(self.ksize)
        W_shape = (self.out_channels, in_channels, kh, kw)
        if not log_sigma2:
            self.W.initialize(W_shape)
        else:
            self.log_sigma2.initialize(W_shape)

    def dropout_convolution_2d(self, x):
        train = configuration.config.train
        W, b = self.W, self.b
        log_alpha = VDF.calculate_log_alpha(
            self.W, self.log_sigma2, eps=1e-8, thresholds=(-8., 8.))
        clip_mask = (log_alpha.data > self.loga_threshold)
        if train:
            W = (1. - clip_mask) * W
            mu = F.convolution_2d(x, (1. - clip_mask) * W, b=None,
                                  stride=self.stride, pad=self.pad)
            si = F.sqrt(
                F.convolution_2d(x * x, F.exp(log_alpha) * W * W, b=None,
                                 stride=self.stride, pad=self.pad) + 1e-8)
            normal_noise = self.xp.random.normal(
                0., 1., mu.shape).astype('f')
            activation = mu + si * normal_noise
            return F.bias(activation, b)
        else:
            return F.convolution_2d(x, (1. - clip_mask) * W, b,
                                    stride=self.stride, pad=self.pad)

    def __call__(self, x):
        if self.W.data is None:
            self._initialize_params(x.shape[1])
        return self.dropout_convolution_2d(x)


class VariationalDropoutTanhRNN(chainer.Chain):

    def __init__(self, in_size, out_size, nobias=False,
                 initialW=None, initial_bias=None,
                 p_threshold=P_THRESHOLD, loga_threshold=LOGA_THRESHOLD,
                 initial_log_sigma2=INITIAL_LOG_SIGMA2):
        W = VariationalDropoutLinear(
            in_size + out_size, out_size, nobias=nobias,
            initialW=initialW, initial_bias=initial_bias,
            p_threshold=p_threshold, loga_threshold=loga_threshold,
            initial_log_sigma2=initial_log_sigma2)
        super(VariationalDropoutTanhRNN, self).__init__(W=W)
        self.in_size = in_size
        self.out_size = out_size
        self.h = None

    def reset_state(self):
        self.h = None

    def set_state(self, h):
        self.h = h

    def __call__(self, x, h=None):
        """RNN call
        If h is given, this works as stateless rnn.
        Otherwise, stateful rnn.
        """
        stateful = (h is None)
        if stateful:
            if self.h is None:
                self.h = self.xp.zeros((x.shape[0], self.out_size)).astype('f')
            h = self.h
        new_h = F.tanh(self.W(F.concat([x, h], axis=1)))

        if stateful:
            self.h = new_h
        else:
            self.h = None

        return new_h


class VariationalDropoutLSTM(chainer.Chain):

    def __init__(self, in_size, out_size, nobias=False,
                 initialW=None, initial_bias=None,
                 p_threshold=P_THRESHOLD, loga_threshold=LOGA_THRESHOLD,
                 initial_log_sigma2=INITIAL_LOG_SIGMA2):
        upward = VariationalDropoutLinear(
            in_size, out_size * 4, nobias=nobias,
            initialW=initialW, initial_bias=initial_bias,
            p_threshold=p_threshold, loga_threshold=loga_threshold,
            initial_log_sigma2=initial_log_sigma2)
        lateral = VariationalDropoutLinear(
            in_size, out_size * 4, nobias=True,
            initialW=initialW,
            p_threshold=p_threshold, loga_threshold=loga_threshold,
            initial_log_sigma2=initial_log_sigma2)
        super(VariationalDropoutLSTM, self).__init__(
            upward=upward, lateral=lateral)
        self.in_size = in_size
        self.out_size = out_size
        self.h = None
        self.c = None

    def reset_state(self):
        self.h = None
        self.c = None

    def set_state(self, c, h):
        self.h = h
        self.c = c

    def __call__(self, x):
        """Stateful LSTM call
        """
        memory_efficiency = configuration.config.user_memory_efficiency

        if memory_efficiency > 2:
            lstm_in = F.forget(self.upward, x)
        else:
            lstm_in = self.upward(x)
        if self.h is not None:
            if memory_efficiency > 2:
                lstm_in += F.forget(self.lateral, x)
            else:
                lstm_in += self.lateral(x)
        if self.c is None:
            self.c = self.xp.zeros((x.shape[0], self.out_size)).astype('f')

        if memory_efficiency > 1:
            self.c, self.h = F.forget(F.lstm, self.c, lstm_in)
        else:
            self.c, self.h = F.lstm(self.c, lstm_in)
        return self.h


def get_vd_link(link,
                p_threshold=P_THRESHOLD, loga_threshold=LOGA_THRESHOLD,
                initial_log_sigma2=INITIAL_LOG_SIGMA2):
    if link._cpu:
        gpu = -1
    else:
        gpu = link._device_id
        link.to_cpu()
    initialW = link.W.data
    initial_bias = getattr(link, 'b', None)
    if initial_bias is not None:
        initial_bias = initial_bias.data
    if type(link) == L.Linear:
        out_size, in_size = link.W.shape
        new_link = VariationalDropoutLinear(
            in_size=in_size, out_size=out_size, nobias=False,
            p_threshold=p_threshold, loga_threshold=loga_threshold,
            initial_log_sigma2=initial_log_sigma2)
    elif type(link) == L.Convolution2D:
        out_channels, in_channels = link.W.shape[:2]
        ksize = link.ksize
        stride = link.stride
        pad = link.pad
        new_link = VariationalDropoutConvolution2D(
            in_channels=in_channels, out_channels=out_channels,
            ksize=ksize, stride=stride, pad=pad,
            nobias=None, initialW=None, initial_bias=None,
            p_threshold=p_threshold, loga_threshold=loga_threshold,
            initial_log_sigma2=initial_log_sigma2)
    else:
        NotImplementedError()
    new_link.W.data[:] = numpy.array(initialW).astype('f')
    assert(numpy.any(link.W.data == new_link.W.data))
    if initial_bias is not None:
        new_link.b.data[:] = numpy.array(initial_bias).astype('f')
        assert(numpy.any(link.b.data == new_link.b.data))
    if gpu >= 0:
        new_link.to_gpu(gpu)
    return new_link


def to_variational_dropout_link(parent, name, link, path_name=''):
    raw_name = name.lstrip('/')
    if isinstance(link, chainer.Chain):
        for child_name, child_link in sorted(
                link.namedlinks(skipself=True), key=lambda x: x[0]):
            to_variational_dropout_link(link, child_name, child_link,
                                        path_name=raw_name + '/')
    elif not '/' in raw_name:
        if not getattr(link, 'is_variational_dropout', False) and \
                type(link) in [L.Linear, L.Convolution2D]:
            new_link = get_vd_link(link.copy())
            delattr(parent, raw_name)
            parent.add_link(raw_name, new_link)
            print(' Replace link {} with a variant using variational dropout.'
                  .format(path_name + raw_name))

        else:
            print('  Retain link {}.'.format(path_name + raw_name))


class VariationalDropoutChain(chainer.link.Chain):

    def __init__(self, warm_up=0.0001, **kwargs):
        super(VariationalDropoutChain, self).__init__(**kwargs)
        self.warm_up = warm_up
        if self.warm_up:
            self.kl_coef = 0.
        else:
            self.kl_coef = 1.

    def calc_loss(self, x, t, add_kl=True, split_loss=False, calc_stats=True):
        train = configuration.config.train
        memory_efficiency = configuration.config.user_memory_efficiency

        self.y = self(x)
        if memory_efficiency > 0:
            self.class_loss = F.forget(F.softmax_cross_entropy, self.y, t)
        else:
            self.class_loss = F.softmax_cross_entropy(self.y, t)

        ignore = False
        if train and self.xp.isnan(self.class_loss.data):
            self.class_loss = chainer.Variable(
                self.xp.array(0.).astype('f').sum())
            ignore = True
        else:
            reporter.report({'class': self.class_loss.data}, self)

        if add_kl:
            a_regf = sum(
                VDF.calculate_kl(
                    link.W, link.loga_threshold,
                    log_sigma2=link.log_sigma2, log_alpha=None,
                    eps=1e-8, thresholds=(-8., 8.))
                for link in self.links()
                if getattr(link, 'is_variational_dropout', False))
            self.kl_loss = a_regf * self.kl_coef

            if train and self.xp.isnan(self.kl_loss.data):
                self.kl_loss = chainer.Variable(
                    self.xp.array(0.).astype('f').sum())
                ignore = True
            else:
                reporter.report({'kl': self.kl_loss.data}, self)
            self.kl_coef = min(self.kl_coef + self.warm_up, 1.)
            reporter.report({'kl_coef': self.kl_coef}, self)

            self.loss = self.class_loss + self.kl_loss
        else:
            self.loss = self.class_loss

        if not ignore:
            reporter.report({'loss': self.loss.data}, self)

        self.accuracy = F.accuracy(self.y.data, t).data
        reporter.report({'accuracy': self.accuracy}, self)

        if calc_stats:
            stats = calculate_stats(self)
            reporter.report({'mean_p': stats['mean_p']}, self)
            reporter.report({'sparsity': stats['sparsity']}, self)
            reporter.report({'W/Wnz': stats['W/Wnz']}, self)

        if split_loss:
            return self.class_loss, self.kl_loss
        else:
            return self.loss

    def to_cpu_sparse(self):
        self.to_cpu()
        n_total_old_params = 0
        n_total_new_params = 0
        if self.xp is not numpy:
            warnings.warn('SparseLinearForwardCPU link is made for'
                          ' inference usage. Please to_cpu()'
                          ' before inference.')
        print('Sparsifying fully-connected linear layer in the model...')
        for name, link in sorted(
                self.namedlinks(skipself=True), key=lambda x: x[0]):
            raw_name = name.lstrip('/')
            n_old_params = sum(p.size for p in link.params())

            if getattr(link, 'is_variational_dropout_linear', False):
                old = link.copy()
                delattr(self, raw_name)
                self.add_link(raw_name, old.get_sparse_cpu_model())
                n_new_params = getattr(self, raw_name).sparse_W.size
                if hasattr(getattr(self, raw_name), 'sparse_b'):
                    n_new_params += getattr(self, raw_name).sparse_b.size
                print(' Sparsified link {}.'.format(raw_name) +
                      '\t# of params: {} -> {} ({:.3f}%)'.format(
                          n_old_params, n_new_params,
                          (n_new_params * 1. / n_old_params * 100)))
                n_total_old_params += n_old_params
                n_total_new_params += n_new_params
            elif not isinstance(link, chainer.Chain):
                print('  Retain link {}.\t# of params: {}'.format(
                    raw_name, n_old_params))
                n_new_params = n_old_params
                n_total_old_params += n_old_params
                n_total_new_params += n_new_params
        print(' total # of params: {} -> {} ({:.3f}%)'.format(
            n_total_old_params, n_total_new_params,
            (n_total_new_params * 1. / n_total_old_params * 100)))

    def to_variational_dropout(self):
        """Make myself to use variational dropout

        Linear -> VariationalDropoutLinear
        Convolution2D -> VariationalDropoutConvolution2D

        """
        print('Make {} to use variational dropout.'.format(
            self.__class__.__mro__[2].__name__))
        for name, link in sorted(
                self.namedlinks(skipself=True), key=lambda x: x[0]):
            to_variational_dropout_link(self, name, link)
