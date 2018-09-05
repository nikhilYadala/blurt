import numpy as np
import collections
import itertools
from . import kernels
from .iir import IIRFilter

Channel = collections.namedtuple('Channel', ['Fs', 'Fc', 'upsample_factor'])

stereoDelay = .005
preemphasisOrder = 0
gap = .05 #.2
nfft = 64
ncp = 16
sts_freq = np.zeros(64, complex)
sts_freq[[4, 8, 12, 16, 20, 24, -24, -20, -16, -12, -8, -4]] = \
    np.array([-1, -1, 1, 1, 1, 1, 1, -1, 1, -1, -1, 1]) * (13./6.)**.5 * (1+1j)
lts_freq = np.array([0, 1,-1,-1, 1, 1,-1, 1,-1, 1,-1,-1,-1,-1,-1, 1,
                     1,-1,-1, 1,-1, 1,-1, 1, 1, 1, 1, 0, 0, 0, 0, 0,
                     0, 0, 0, 0, 0, 0, 1, 1,-1,-1, 1, 1,-1, 1,-1, 1,
                     1, 1, 1, 1, 1,-1,-1, 1, 1,-1, 1,-1, 1, 1, 1, 1])
ts_reps = 2
dataSubcarriers = np.r_[-26:-21,-20:-7,-6:0,1:7,8:21,22:27]
pilotSubcarriers = np.array([-21,-7,7,21])
pilotTemplate = np.array([1,1,1,-1])

############################ Scrambler ############################

scrambler = np.zeros((128,127), np.uint8)
s = np.arange(128, dtype=np.uint8)
for i in range(127):
    s = np.uint8((s << 1) ^ (1 & ((s >> 3) ^ (s >> 6))))
    scrambler[:,i] = s & 1

############################ CRC ############################

def CRC(x):
    a = np.r_[np.r_[np.zeros(x.size, int), np.ones(32, int)] ^ \
              np.r_[np.zeros(32, int), x[::-1]], np.zeros(-x.size%16, int)]
    a = (a.reshape(-1, 16) << np.arange(16)).sum(1)[::-1]
    return kernels.crc(a, lut)

lut = np.arange(1<<16, dtype=np.uint64) << 32
for i in range(47,31,-1):
    lut ^= (0x104c11db7 << (i-32)) * ((lut >> i) & 1)
lut = lut.astype(np.uint32)

############################ CC ############################

def mul(a, b):
    return np.bitwise_xor.reduce(a[:,None,None] * (b[:,None] & (1<<np.arange(7))), -1)

output_map = 1 & (mul(np.array([109, 79]), np.arange(128)) >> 6)
output_map_soft = output_map * 2 - 1

def encode(y):
    return kernels.encode(y, output_map)

def decode(llr):
    N = llr.size//2
    return kernels.decode(N, (llr[:N*2].reshape(-1,2,1)*output_map_soft).sum(1))

############################ Rates ############################

class Rate:
    def __init__(self, Nbpsc, ratio):
        self.Nbpsc = Nbpsc
        if Nbpsc == 1:
            self.symbols = np.array([-1,1])
        else:
            n = Nbpsc//2
            grayRevCode = sum(((np.arange(1<<n) >> i) & 1) << (n-1-i) for i in range(n))
            grayRevCode ^= grayRevCode >> 1
            grayRevCode ^= grayRevCode >> 2
            symbols = (2*grayRevCode+1-(1<<n)) * (1.5 / ((1<<Nbpsc) - 1))**.5
            self.symbols = np.tile(symbols, 1<<n) + 1j*np.repeat(symbols, 1<<n)
        self.puncturingMatrix = np.bool_({(1,2):[1,1], (2,3):[1,1,1,0], (3,4):[1,1,1,0,0,1],
                                          (5,6):[1,1,1,0,0,1,1,0,0,1]}[ratio])
        self.ratio = ratio
    def depuncture(self, y):
        output_size = (y.size + self.ratio[1]-1) // self.ratio[1] * self.ratio[0] * 2
        output = np.zeros(output_size, y.dtype)
        output[np.resize(self.puncturingMatrix, output.size)] = y
        return output
    def demap(self, y, dispersion):
        n = self.Nbpsc
        squared_distance = np.abs(self.symbols - y.flatten()[:,None])**2
        ll = -np.log(np.pi * dispersion) - squared_distance / dispersion
        ll -= np.logaddexp.reduce(ll, 1)[:,None]
        j = np.arange(1<<n)
        llr = np.zeros((y.size, n), int)
        for i in range(n):
            llr[:,i] = 10 * (np.logaddexp.reduce(ll[:,0 != (j & (1<<i))], 1) - \
                             np.logaddexp.reduce(ll[:,0 == (j & (1<<i))], 1))
        return np.clip(llr, -1e4, 1e4)

rates = {0xb: Rate(1, (1,2)), 0xf: Rate(2, (1,2)), 0xa: Rate(2, (3,4)), 0xe: Rate(4, (1,2)),
         0x9: Rate(4, (3,4)), 0xd: Rate(6, (2,3)), 0x8: Rate(6, (3,4)), 0xc: Rate(6, (5,6))}

############################ OFDM ############################

Nsc = dataSubcarriers.size
Nsc_used = dataSubcarriers.size + pilotSubcarriers.size
N_sts_period = nfft // 4
N_sts_samples = ts_reps * (ncp + nfft)
N_sts_reps = N_sts_samples // N_sts_period
N_training_samples = N_sts_samples + ts_reps * (ncp + nfft) + 8

def interleave(input, Ncbps, Nbpsc, reverse=False):
    s = max(Nbpsc//2, 1)
    j = np.arange(Ncbps)
    if reverse:
        i = (Ncbps//16) * (j%16) + (j//16)
        p = s*(i//s) + (i + Ncbps - (16*i//Ncbps)) % s
    else:
        i = s*(j//s) + (j + (16*j//Ncbps)) % s
        p = 16*i - (Ncbps - 1) * (16*i//Ncbps)
    return input[...,p].flatten()

class Autocorrelator:
    def __init__(self):
        self.y_hist = np.zeros(0)
        self.results = []
    def feed(self, y):
        y = np.r_[self.y_hist, y]
        count_needed = y.size // N_sts_period * N_sts_period
        count_consumed = count_needed - N_sts_reps * N_sts_period
        if count_consumed <= 0:
            self.y_hist = y
        else:
            self.y_hist = y[count_consumed:]
            y = y[:count_needed].reshape(-1, N_sts_period)
            corr_sum = np.abs((y[:-1].conj() * y[1:]).sum(1)).cumsum()
            self.results.append(corr_sum[N_sts_reps-1:] - corr_sum[:-N_sts_reps+1])
    def __iter__(self):
        while self.results:
            yield self.results.pop()

class PeakDetector:
    def __init__(self, l):
        self.y_hist = np.zeros(0)
        self.l = l
        self.i = l
        self.results = []
    def feed(self, y):
        y = np.r_[self.y_hist, y]
        count_needed = y.size
        count_consumed = count_needed - 2*self.l
        if count_consumed <= 0:
            self.y_hist = y
        else:
            self.y_hist = y[count_consumed:]
            stripes_shape = (2*self.l+1, count_needed-2*self.l)
            stripes_strides = (y.strides[0],)*2
            stripes = np.lib.stride_tricks.as_strided(y, stripes_shape, stripes_strides)
            self.results.extend((stripes.argmax(0) == self.l).nonzero()[0] + self.i)
            self.i += count_consumed
    def __iter__(self):
        while self.results:
            yield self.results.pop()

def estimate_cfo(y, overlap, span):
    return np.angle((y[:-overlap].conj() * y[overlap:]).sum()) / span

def remove_cfo(y, k, theta):
    return y * np.exp(-1j*theta*np.r_[k:k+y.size])

def downconvert(source_queue, channel):
    i = 0
    lp = IIRFilter.lowpass(.45/channel.upsample_factor)
    while True:
        y = source_queue.get()
        if y is None:
            break
        smoothed = lp(y * np.exp(-1j*2*np.pi*channel.Fc/channel.Fs * np.r_[i:i+y.size]))
        yield smoothed[-i%channel.upsample_factor::channel.upsample_factor]
        i += y.size

def train(y):
    i = 0
    theta = estimate_cfo(y[i:i+N_sts_samples], N_sts_period, N_sts_period)
    i += N_sts_samples + ncp*ts_reps
    lts = np.fft.fft(remove_cfo(y[i:i+nfft*ts_reps], i, theta).reshape(-1, nfft), axis=1)
    theta += estimate_cfo(lts * (lts_freq != 0), 1, nfft)
    def wienerFilter(i):
        lts = np.fft.fft(remove_cfo(y[i:i+nfft*ts_reps], i, theta).reshape(-1, nfft), axis=1)
        Y = lts.mean(0)
        S_Y = (np.abs(lts)**2).mean(0)
        G = Y.conj()*lts_freq / S_Y
        snr = 1./(G*lts - lts_freq).var()
        return snr, i + nfft*ts_reps, G
    snr, i, G = max(map(wienerFilter, range(i-8, i+8)))
    var_input = y[:N_training_samples].var()
    var_n = var_input / (snr * Nsc_used / Nsc + 1)
    var_x = var_input - var_n
    var_y = 2*var_n*var_x + var_n**2
    uncertainty = np.arctan(var_y**.5 / var_x) / nfft**.5
    var_ni = var_x/Nsc_used/snr
    return (G, uncertainty, var_ni, theta), i

def decodeOFDM(syms, i, training_data):
    G, uncertainty, var_ni, theta_cfo = training_data
    Np = pilotSubcarriers.size
    sigma_noise = Np*var_ni*.5
    sigma = sigma_noise + Np*np.sin(uncertainty)**2
    P = np.diag([sigma, sigma, uncertainty**2])
    x = Np * np.array([[1.,0.,0.]]).T
    R = np.diag([sigma_noise, sigma_noise])
    Q = P * 0.1
    for j, y in enumerate(syms):
        sym = np.fft.fft(remove_cfo(y[ncp:], i+ncp, theta_cfo)) * G
        i += nfft+ncp
        pilot = (sym[pilotSubcarriers]*pilotTemplate).sum() * (1.-2.*scrambler[0x7F,j%127])
        re,im,theta = x[:,0]
        c, s = np.cos(theta), np.sin(theta)
        F = np.array([[c, -s, -s*re - c*im], [s, c, c*re - s*im], [0, 0, 1]])
        x[0,0] = c*re - s*im
        x[1,0] = c*im + s*re
        P = F.dot(P).dot(F.T) + Q
        S = P[:2,:2] + R
        K = np.linalg.solve(S, P[:2,:]).T
        x += K.dot(np.array([[pilot.real], [pilot.imag]]) - x[:2,:])
        P -= K.dot(P[:2,:])
        u = x[0,0] - x[1,0]*1j
        yield sym[dataSubcarriers] * (u/abs(u))

class OneShotDecoder:
    def __init__(self, i):
        self.start = i
        self.j = 0
        max_coded_bits = ((2 + mtu + 4) * 8 + 6) * 2
        max_data_symbols = (max_coded_bits+Nsc-1) // Nsc
        max_samples = N_training_samples + (ncp+nfft) * (1 + max_data_symbols)
        self.y = np.full(max_samples, np.inf, complex)
        self.size = 0
        self.trained = False
        self.result = None
    def feed(self, sequence, k):
        if self.result is not None:
            return
        if k < self.start:
            sequence = sequence[self.start-k:]
        self.y[self.size:self.size+sequence.size] = sequence[:self.y.size-self.size]
        self.size += sequence.size
        if not self.trained:
            if self.size > N_training_samples:
                self.training_data, self.i = train(self.y)
                syms = self.y[self.i:self.i+(self.y.size-self.i)//(nfft+ncp)*(nfft+ncp)]
                self.syms = decodeOFDM(syms.reshape(-1, (nfft+ncp)), self.i, self.training_data)
                self.trained = True
            else:
                return
        j_valid = (self.size - self.i) // (nfft + ncp)
        if self.j == 0:
            if j_valid > 0:
                lsig = next(self.syms)
                lsig_bits = decode(interleave(lsig.real, Nsc, 1, True))
                if not int(lsig_bits.sum()) & 1 == 0:
                    self.result = ()
                    return
                lsig_bits = (lsig_bits[:18] << np.arange(18)).sum()
                if not lsig_bits & 0xF in rates:
                    self.result = ()
                    return
                self.rate = rates[lsig_bits & 0xF]
                length_octets = (lsig_bits >> 5) & 0xFFF
                if length_octets > mtu:
                    self.result = ()
                    return
                self.length_coded_bits = (length_octets*8 + 16+6)*2
                self.Ncbps = Nsc * self.rate.Nbpsc
                self.length_symbols = int((self.length_coded_bits+self.Ncbps-1) // self.Ncbps)
                plcp_coded_bits = interleave(encode((lsig_bits >> np.arange(24)) & 1), Nsc, 1)
                self.dispersion = (lsig-(plcp_coded_bits*2.-1.)).var()
                self.j = 1
            else:
                return
        if j_valid < self.length_symbols + 1:
            return
        syms = np.array(list(itertools.islice(self.syms, self.length_symbols)))
        demapped_bits = self.rate.demap(syms, self.dispersion).reshape(-1, self.Ncbps)
        deinterleaved_bits = interleave(demapped_bits, self.Ncbps, self.rate.Nbpsc, True)
        llr = self.rate.depuncture(deinterleaved_bits)[:self.length_coded_bits]
        output_bits = (decode(llr) ^ np.resize(scrambler[0x5d], llr.size//2))[16:-6]
        if not CRC(output_bits) == 0xc704dd7b:
            self.result = ()
            return
        output_bytes = (output_bits[:-32].reshape(-1, 8) << np.arange(8)).sum(1)
        self.result = output_bytes.astype(np.uint8).tostring(), 10*np.log10(1/self.dispersion)

class Decoder:
    def __init__(self, source_queue, channel):
        self.source_queue = source_queue
        self.channel = channel
    def __iter__(self):
        autocorrelator = Autocorrelator()
        peakDetector = PeakDetector(25)
        lookback = deque()
        decoders = []
        k_current = 0
        k_lookback = 0
        for sequence in downconvert(self.source_queue, self.channel):
            autocorrelator.feed(sequence)
            for y in autocorrelator:
                peakDetector.feed(y)
            for peak in peakDetector:
                d = OneShotDecoder(peak * N_sts_period + 16)
                k = k_lookback
                for y in lookback:
                    d.feed(y, k)
                    k += y.size
                decoders.append(d)
            lookback.append(sequence)
            for d in decoders:
                d.feed(sequence, k_current)
                if d.result is not None:
                    if d.result:
                        yield d.result
                    decoders.remove(d)
            k_current += sequence.size
            while lookback and k_lookback+lookback[0].size < k_current - 1024:
                k_lookback += lookback.popleft().size

def subcarriersFromBits(bits, rate, scramblerState):
    Ncbps = Nsc * rate.Nbpsc
    Nbps = Ncbps * rate.ratio[0] // rate.ratio[1]
    pad_bits = 6 + -(bits.size + 6) % Nbps
    scrambled = np.r_[bits, np.zeros(pad_bits, int)] ^ \
                np.resize(scrambler[scramblerState], bits.size + pad_bits)
    scrambled[bits.size:bits.size+6] = 0
    punctured = encode(scrambled)[np.resize(rate.puncturingMatrix, scrambled.size*2)]
    interleaved = interleave(punctured.reshape(-1, Ncbps), Nsc * rate.Nbpsc, rate.Nbpsc)
    grouped = (interleaved.reshape(-1, rate.Nbpsc) << np.arange(rate.Nbpsc)).sum(1)
    return rate.symbols[grouped].reshape(-1, Nsc)

class Encoder:
    def __init__(self, source_queue, channel):
        self.source_queue = source_queue
        self.channel = channel
    def __iter__(self):
        channel = self.channel
        cutoff = (Nsc_used/2 + .5)/nfft
        lp1 = IIRFilter.lowpass(cutoff/channel.upsample_factor)
        lp2 = lp1.copy()
        baseRate = rates[0xb]
        k = 0
        while True:
            octets = self.source_queue.get()
            if octets is None:
               break
            # prepare header and payload bits
            rateEncoding = 0xb
            rate = rates[rateEncoding]
            data_bits = (octets[:,None] >> np.arange(8)[None,:]).flatten() & 1
            data_bits = np.r_[np.zeros(16, int), data_bits,
                              (~CRC(data_bits) >> np.arange(32)[::-1]) & 1]
            plcp_bits = ((rateEncoding | ((octets.size+4) << 5)) >> np.arange(18)) & 1
            plcp_bits[-1] = plcp_bits.sum() & 1
            # OFDM modulation
            subcarriers = np.vstack((subcarriersFromBits(plcp_bits, baseRate, 0   ),
                                     subcarriersFromBits(data_bits, rate,     0x5d)))
            pilotPolarity = np.resize(scrambler[0x7F], subcarriers.shape[0])
            symbols = np.zeros((subcarriers.shape[0],nfft), complex)
            symbols[:,dataSubcarriers] = subcarriers
            symbols[:,pilotSubcarriers] = pilotTemplate * (1. - 2.*pilotPolarity)[:,None]
            ts_tile_shape = (ncp*ts_reps+nfft-1)//nfft + ts_reps + 1
            symbols_tile_shape = (1, (ncp+nfft-1)//nfft + 1 + 1)
            sts_time = np.tile(np.fft.ifft(sts_freq), ts_tile_shape)[-ncp*ts_reps%nfft:-nfft+1]
            lts_time = np.tile(np.fft.ifft(lts_freq), ts_tile_shape)[-ncp*ts_reps%nfft:-nfft+1]
            symbols  = np.tile(np.fft.ifft(symbols ), symbols_tile_shape)[:,-ncp%nfft:-nfft+1]
            # temporal smoothing
            subsequences = [sts_time, lts_time] + list(symbols)
            output = np.zeros(sum(map(len, subsequences)) - len(subsequences) + 1, complex)
            i = 0
            for x in subsequences:
                j = i + len(x)-1
                output[i] += .5*x[0]
                output[i+1:j] += x[1:-1]
                output[j] += .5*x[-1]
                i = j
            output = np.vstack((output, np.zeros((channel.upsample_factor-1,
                                                  output.size), output.dtype)))
            output = output.T.flatten()*channel.upsample_factor
            output = lp2(lp1(np.r_[np.zeros(200), output, np.zeros(200)]))
            # modulation and pre-emphasis
            Omega = 2*np.pi*channel.Fc/channel.Fs
            output = (output * np.exp(1j* Omega * np.r_[k:k+output.size])).real
            k += output.size
            for i in range(preemphasisOrder):
                output = np.diff(np.r_[output,0])
            output *= abs(np.exp(1j*Omega)-1)**-preemphasisOrder
            output /= abs(output).max()
            # stereo beamforming reduction
            delay = np.zeros(int(stereoDelay*channel.Fs))
            pause = np.zeros(int(gap*channel.Fs))
            yield np.vstack((np.r_[delay, output, pause], np.r_[output, delay, pause])).T
