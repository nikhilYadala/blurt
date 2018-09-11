#!/usr/bin/env python
import numpy as np
from . import scrambler
from . import cc
from .interleaver import interleave

def estimate_cfo(y, overlap, span):
    return np.angle((y[:-overlap].conj() * y[overlap:]).sum()) / span

def downconvert(y, k, Omega):
    return y * np.exp(-1j*Omega*np.r_[k:k+y.shape[0]])[:,None]

class OFDM:
    def encodeSymbols(self, symbols, oversample, reps=1, axis=-1):
        nfft = self.nfft
        ncp = self.ncp
        ndim = np.ndim(symbols)
        if axis < 0:
            axis += ndim
        # stuff zeros in the middle of the specified axis to pad it to length nfft * oversample
        front_index = (slice(None),) * axis + (slice(None,nfft//2),) + (slice(None),) * (ndim-axis-1)
        back_index = (slice(None),) * axis + (slice(nfft//2,None),) + (slice(None),) * (ndim-axis-1)
        zero_shape = symbols.shape[:axis] + (nfft*(oversample-1),) + symbols.shape[axis+1:]
        symbols = np.concatenate((symbols[front_index], np.zeros(zero_shape), symbols[back_index]), axis=axis)
        # perform ifft
        symbols = np.fft.ifft(symbols, axis=axis)*oversample
        # perform cyclic extension
        num_tiles = (ncp*reps-1) // nfft + reps + 2
        zero_pos = ((ncp*reps-1) // nfft + 1) * nfft
        start = zero_pos - ncp * reps
        stop = zero_pos + nfft * reps + 1
        tile_shape = (1,) * axis + (num_tiles,) + (1,) * (ndim-axis-1)
        extract_index = (slice(None),) * axis + (slice(start*oversample, stop*oversample),) + (slice(None),) * (ndim-axis-1)
        return np.tile(symbols, tile_shape)[extract_index]

    def blendSymbols(self, subsequences, oversample):
        output = np.zeros(sum(map(len, subsequences)) - (len(subsequences) - 1) * oversample, complex)
        i = 0
        ramp = np.linspace(0,1,2+oversample)[1:-1]
        for x in subsequences:
            weight = np.ones(x.size)
            weight[-1:-oversample-1:-1] = weight[:oversample] = ramp
            output[i:i+len(x)] += weight * x
            i += len(x) - oversample
        return output

    @property
    def nsym(self):
        return self.ncp + self.nfft

    @property
    def N_sts_samples(self):
        return self.ts_reps * self.nsym

    @property
    def N_training_samples(self):
        return self.N_sts_samples + self.ts_reps * self.nsym + 8

    def train(self, y):
        nfft = self.nfft
        ncp = self.ncp
        ts_reps = self.ts_reps
        N_sts_samples = self.N_sts_samples
        N_sts_period = nfft // 4
        Nss = y.shape[1]
        i = 0
        Omega = estimate_cfo(y[i:i+N_sts_samples], N_sts_period, N_sts_period)
        i += N_sts_samples + ncp*ts_reps
        lts = np.fft.fft(downconvert(y[i:i+nfft*ts_reps], i, Omega).reshape(-1, nfft, Nss), axis=1)
        Omega += estimate_cfo(lts * (self.lts_freq != 0)[:,None], 1, nfft)
        def wienerFilter(i):
            lts = np.fft.fft(downconvert(y[i:i+nfft*ts_reps], i, Omega).reshape(-1, nfft, Nss), axis=1)
            X = self.lts_freq[:,None]
            Y = lts.sum(0)
            YY = (lts[:,:,:,None] * lts[:,:,None,:].conj()).sum(0)
            YY_inv = np.linalg.pinv(YY, 1e-3)
            G = np.einsum('ij,ik,ikl->ijl', X, Y.conj(), YY_inv)
            snr = Nss/(abs(np.einsum('ijk,lik->lij', G, lts) - X[None,:,:])**2).mean()
            return snr, i + nfft*ts_reps, G
        snr, i, G = max(map(wienerFilter, range(i-8, i+8)))
        i_sts_start = i - ncp*ts_reps - N_sts_samples
        i_lts_end = i + nfft*ts_reps
        var_input = y[i_sts_start:i_lts_end].var()
        var_n = var_input / (snr / Nss * self.Nsc_used / self.Nsc + 1)
        var_x = var_input - var_n
        var_y = 2*var_n*var_x + var_n**2
        uncertainty = np.arctan(var_y**.5 / var_x) / nfft**.5
        var_ni = var_x/self.Nsc_used*Nss/snr
        return (G, uncertainty, var_ni, Omega), i

    def ekfDecoder(self, syms, i, training_data):
        nsym = self.nsym
        G, uncertainty, var_ni, theta_cfo = training_data
        Np = self.pilotSubcarriers.size
        sigma_noise = Np*var_ni*.5
        sigma = sigma_noise + Np*np.sin(uncertainty)**2
        P = np.diag([sigma, sigma, uncertainty**2])
        x = Np * np.array([[1.,0.,0.]]).T
        R = np.diag([sigma_noise, sigma_noise])
        Q = P * 0.1
        for j, y in enumerate(syms):
            sym = np.einsum('ijk,ik->ij', G, np.fft.fft(downconvert(y[self.ncp:], i+self.ncp, theta_cfo), axis=0))[:,0]
            i += nsym
            pilot = (sym[self.pilotSubcarriers]*self.pilotTemplate).sum() * float(1-2*scrambler.pilot_sequence[j%127])
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
            yield sym[self.dataSubcarriers] * (u/abs(u))

    def subcarriersFromBits(self, bits, rate, scramblerState):
        # adds tail bits and any needed padding to form a full symbol; does not add SERVICE
        Ncbps = self.Nsc * rate.Nbpsc
        Nbps = Ncbps * rate.ratio[0] // rate.ratio[1]
        pad_bits = 6 + -(bits.size + 6) % Nbps
        scrambled = scrambler.scramble(np.r_[bits, np.zeros(pad_bits, int)], scramblerState)
        scrambled[bits.size:bits.size+6] = 0
        punctured = cc.encode(scrambled)[np.resize(rate.puncturingMatrix, scrambled.size*2)]
        interleaved = interleave(punctured, self.Nsc * rate.Nbpsc, rate.Nbpsc)
        grouped = (interleaved.reshape(-1, rate.Nbpsc) << np.arange(rate.Nbpsc)).sum(1)
        return rate.constellation[0].symbols[grouped].reshape(-1, self.Nsc)

class L(OFDM): # Legacy (802.11a) mode
    def __init__(self):
        super().__init__()
        self.nfft = 64
        self.ncp = 16
        self.ts_reps = 2
        self.sts_freq = np.zeros(64, np.complex128)
        self.sts_freq[[4, 8, 12, 16, 20, 24, -24, -20, -16, -12, -8, -4]] = \
            np.array([-1, -1, 1, 1, 1, 1, 1, -1, 1, -1, -1, 1]) * (13./6.)**.5 * (1+1j)
        self.lts_freq = np.array([
            0, 1,-1,-1, 1, 1,-1, 1,-1, 1,-1,-1,-1,-1,-1, 1,
            1,-1,-1, 1,-1, 1,-1, 1, 1, 1, 1, 0, 0, 0, 0, 0,
            0, 0, 0, 0, 0, 0, 1, 1,-1,-1, 1, 1,-1, 1,-1, 1,
            1, 1, 1, 1, 1,-1,-1, 1, 1,-1, 1,-1, 1, 1, 1, 1])
        self.dataSubcarriers = np.r_[-26:-21,-20:-7,-6:0,1:7,8:21,22:27]
        self.pilotSubcarriers = np.array([-21,-7,7,21])
        self.pilotTemplate = np.array([1,1,1,-1])
        self.Nsc = self.dataSubcarriers.size
        self.Nsc_used = self.dataSubcarriers.size + self.pilotSubcarriers.size

    def encode(self, parts, oversample):
        signal, data = parts
        subcarriers = np.concatenate((signal, data), axis=0)
        pilotPolarity = np.resize(scrambler.pilot_sequence, subcarriers.shape[0])
        symbols = np.zeros((subcarriers.shape[0], self.nfft), complex)
        symbols[:,self.dataSubcarriers] = subcarriers
        symbols[:,self.pilotSubcarriers] = self.pilotTemplate * (1. - 2.*pilotPolarity)[:,None]
        sts_time = self.encodeSymbols(self.sts_freq, oversample, self.ts_reps, -1)
        lts_time = self.encodeSymbols(self.lts_freq, oversample, self.ts_reps, -1)
        symbols  = self.encodeSymbols(symbols, oversample, 1, -1)
        return self.blendSymbols([sts_time, lts_time] + list(symbols), oversample)

class HT20(L): # High Throughput 20 MHz bandwidth
    def __init__(self, short_GI=False):
        super().__init__()
        self.ncp = 8 if short_GI else 16
        self.lts_freq.put([27, 28, -28, -27], [-1, -1, 1, 1])
        self.dataSubcarriers = np.r_[-28:-26, self.dataSubcarriers, 27:29]
        self.Nsc += 4
        self.Nsc_used += 4
        self.pilotTemplates = { # indexed by N_sts, then by sts
            1:np.array([[ 1, 1, 1,-1]]),
            2:np.array([[ 1, 1,-1,-1],
                        [ 1,-1,-1, 1]]),
            3:np.array([[ 1, 1,-1,-1],
                        [ 1,-1, 1,-1],
                        [-1, 1, 1,-1]]),
            4:np.array([[ 1, 1, 1,-1],
                        [ 1, 1,-1, 1],
                        [ 1,-1, 1, 1],
                        [-1, 1, 1, 1]]),
        }

class HT40(L): # High Throughput 40 MHz bandwidth
    def __init__(self, short_GI=False):
        super().__init__()
        self.nfft *= 2
        self.ncp = 16 if short_GI else 32
        self.sts_freq = np.r_[np.fft.fftshift(self.sts_freq), np.fft.fftshift(self.sts_freq)] # factor of j?
        self.lts_freq = np.r_[np.fft.fftshift(self.lts_freq), np.fft.fftshift(self.lts_freq)] # factor of j?
        self.lts_freq.put([-32, -5, -4, -3, -2, 2, 3, 4, 5, 32], [1, -1, -1, -1, 1, -1, 1, 1, -1, 1]) # factor of j?
        self.dataSubcarriers = np.r_[-58:-53,-52:-25,-24:-11,-10:-1,2:11,12:25,26:53,54:59]
        self.pilotSubcarriers = np.array([-53,-25,-11,11,25,53])
        self.Nsc = self.dataSubcarriers.size
        self.pilotTemplates = { # indexed by N_sts, then by sts
            1:np.array([[ 1, 1, 1,-1,-1, 1]]),
            2:np.array([[ 1, 1,-1,-1,-1,-1],
                        [ 1, 1, 1,-1, 1, 1]]),
            3:np.array([[ 1, 1,-1,-1,-1,-1],
                        [ 1, 1, 1,-1, 1, 1],
                        [ 1,-1, 1,-1,-1, 1]]),
            4:np.array([[ 1, 1,-1,-1,-1,-1],
                        [ 1, 1, 1,-1, 1, 1],
                        [ 1,-1, 1,-1,-1, 1],
                        [-1, 1, 1, 1,-1, 1]]),
        }

class HT20_400ns(HT20):
    ncp = 8

class HT20_800ns(HT20):
    ncp = 16

L = L()
HT40_400ns = HT40(True)
HT40_800ns = HT40(False)
