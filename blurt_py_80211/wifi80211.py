#!/usr/bin/env python
import numpy as np
import util, cc, ofdm, scrambler, interleaver, qam, crc

code = cc.ConvolutionalCode()
ofdm = ofdm.OFDM()

class Rate:
    def __init__(self, encoding, (Nbpsc, constellation), (puncturingRatio, puncturingMatrix)):
        self.encoding = encoding
        self.Nbpsc = Nbpsc
        self.constellation = constellation
        self.puncturingRatio = puncturingRatio
        self.puncturingMatrix = puncturingMatrix
        self.linkRate = 12 * Nbpsc * puncturingRatio[0] / puncturingRatio[1]
        self.Nbps = ofdm.Nsc * Nbpsc * puncturingRatio[0] / puncturingRatio[1]
        self.Ncbps = ofdm.Nsc * self.Nbpsc

def autocorrelate(input):
    autocorr = input[16:] * input[:-16].conj()
    autocorr = autocorr[:16*(autocorr.size//16)].reshape(autocorr.size//16, 16).sum(1)
    return np.convolve(np.abs(autocorr), np.ones(9), 'same')

class WiFi_802_11:
    def __init__(self):
        self.rates = [Rate(0xd, qam.bpsk , cc.puncturingSchedule[(1,2)]), Rate(0xf, qam.qpsk , cc.puncturingSchedule[(1,2)]),
                      Rate(0x5, qam.qpsk , cc.puncturingSchedule[(3,4)]), Rate(0x7, qam.qam16, cc.puncturingSchedule[(1,2)]),
                      Rate(0x9, qam.qam16, cc.puncturingSchedule[(3,4)]), Rate(0xb, qam.qam64, cc.puncturingSchedule[(2,3)]),
                      Rate(0x1, qam.qam64, cc.puncturingSchedule[(3,4)]), Rate(0x3, qam.qam64, cc.puncturingSchedule[(5,6)])]

    def plcp_bits(self, rate, octets):
        plcp_rate = util.rev(rate.encoding, 4)
        plcp = plcp_rate | (octets << 5)
        parity = (util.mul(plcp, 0x1FFFF) >> 16) & 1
        plcp |= parity << 17
        return util.shiftout(np.array([plcp]), 18)

    def subcarriersFromBits(self, bits, rate, scramblerState):
        # scrambled includes tail & padding
        scrambled = scrambler.scramble(bits, rate.Nbps, scramblerState=scramblerState)
        coded = code.encode(scrambled)
        punctured = code.puncture(coded, rate.puncturingMatrix)
        interleaved = interleaver.interleave(punctured, rate.Ncbps, rate.Nbpsc)
        return qam.encode(interleaved, rate, ofdm.Nsc)

    def encode(self, input_octets, rate_index):
        service_bits = np.zeros(16, int)
        data_bits = util.shiftout(input_octets, 8)
        data_bits = np.r_[service_bits, data_bits, crc.FCS(data_bits)]
        signal_subcarriers = self.subcarriersFromBits(self.plcp_bits(self.rates[rate_index], input_octets.size+4), self.rates[0], 0)
        data_subcarriers = self.subcarriersFromBits(data_bits, self.rates[rate_index], 0x5d)
        return ofdm.encode(signal_subcarriers, data_subcarriers)

    def train(self, input, lsnr):
        snr = 10.**(.1*lsnr)
        freq_off_estimate = -np.angle(np.sum(input[:144] * input[16:160].conj()))/(2*np.pi*.8e-6)
        input *= np.exp(-2*np.pi*1j*freq_off_estimate*np.arange(input.size)/20e6)
        #err = abs(freq_off_estimate - freq_offset)
        #print 'Coarse frequency estimation error: %.0f Hz (%5.3f bins, %5.3f cyc/sym)' % (err, err / (20e6/64), err * 4e-6)
        offset = 8
        offset += 160
        lts1 = np.fft.fft(input[offset+16:offset+16+64])
        lts2 = np.fft.fft(input[offset+16+64:offset+16+128])
        # y = (x+n1) * (a x+n2).conj()
        # E[y]
        # = E[abs(x)**2*a.conj() + x*n2.conj() + n1*x.conj() + n1*n2]
        # = var(x) * a.conj()
        # so the negative angle of this is the arg of a
        # var(y)
        # = var(x*n2. + n1*x. + n1*n2)
        # = E[(x n2. + n1 x. + n1 n2)(x. n2 + n1. x + n1. n2.)]
        # = 2var(n)var(x) + var(n)^2
        additional_freq_off_estimate = -np.angle(np.sum(lts1[np.where(ofdm.lts_freq)]*lts2[np.where(ofdm.lts_freq)].conj()))/(2*np.pi*3.2e-6)
        var_x = np.var(input)/(64./52./snr + 1)
        var_n = var_x*64./52./snr
        freq_off_estimate += additional_freq_off_estimate
        input *= np.exp(-2*np.pi*1j*additional_freq_off_estimate*np.arange(input.size)/20e6)
        uncertainty = np.arctan(((2*var_n*var_x+var_n**2)**.5) / var_x) / (2*np.pi*3.2e-6) / 64.**.5
        #err = abs(freq_off_estimate - freq_offset)
        # first print error, then print 1.5 sigma for the "best we can really expect"
        #print 'Fine frequency estimation error: %.0f +/- %.0f Hz (%5.3f bins, %5.3f cyc/sym)' % (err, 1.5*uncertainty, err / (20e6/64), err * 4e-6)
        lts1 = np.fft.fft(input[offset+16:offset+16+64])
        lts2 = np.fft.fft(input[offset+16+64:offset+16+128])
        HX = .5*(lts1+lts2)
        S_X = np.abs(ofdm.lts_freq)**2
        H = np.where(S_X, HX/np.where(S_X, ofdm.lts_freq, 1.), 0)
        S_N = np.ones(64) * np.var(HX) / (1+snr)
        G = H.conj()*S_X / (np.abs(HX)**2 + S_N)
        var_x = np.var(input)/(64./52./snr + 1)/52.
        var_n = var_x/snr
        offset += 160
        return input[offset:], (G, uncertainty, var_n)

    def kalman_init(self, uncertainty, var_n):
        std_theta = 2*np.pi*uncertainty*4e-6 # convert from Hz to radians/symbol
        sigma_noise = 4*var_n*.5 # var_n/2 noise per channel, times 4 pilots
        sigma_re = sigma_noise + 4*np.sin(std_theta)**2 # XXX suspect
        sigma_im = sigma_noise + 4*np.sin(std_theta)**2 # XXX suspect
        sigma_theta = std_theta**2
        P = np.matrix(np.diag(np.array([sigma_re, sigma_im, sigma_theta]))) # PAI 2013-02-12 calculation of P[0|0] verified
        x = np.matrix([[4.],[0.],[0.]]) # PAI 2013-02-12 calculation of x[0|0] verified
        Q = P * 0.1 # XXX PAI 2013-02-12 calculation of Q[k] suspect
        I = np.matrix(np.eye(3))
        H = I[:2] # PAI 2013-02-12 calculation of H[k] verified
        R = np.matrix(np.diag(np.array([sigma_noise, sigma_noise]))) # PAI 2013-02-12 calculation of R[k] verified
        return (P, x, Q, H, R, I)

    def kalman_update(self, (P, x, Q, H, R, I), pilot):
        # extended kalman filter
        re = x[0,0]
        im = x[1,0]
        theta = x[2,0]
        c = np.cos(theta)
        s = np.sin(theta)
        F = np.matrix([[c, -s, -s*re - c*im], [s,  c,  c*re - s*im], [0,  0,  1]]) # PAI 2013-02-12 calculation of F[k-1] verified
        x[0,0] = c*re - s*im # PAI 2013-02-12 calculation of x[k|k-1] verified
        x[1,0] = c*im + s*re
        P = F * P * F.T + Q # PAI 2013-02-12 calculation of P[k|k-1] verified
        z = np.matrix([[pilot.real], [pilot.imag]]) # PAI 2013-02-12 calculation of z[k] verified
        y = z - H * x # PAI 2013-02-12 calculation of y[k] verified
        S = H * P * H.T + R # PAI 2013-02-12 calculation of S[k] verified
        try:
            K = P * H.T * S.I # PAI 2013-02-12 calculation of K[k] verified
        except LinAlgError:
            # singular S means P has become negative definite
            K = 0
            # oh well, abs() its eigenvalues :-P
            U, V = np.eigh(P)
            P = V * np.diag(np.abs(U)) * V.H
            print >> sys.stderr, 'Singular K'
        x += K * y # PAI 2013-02-12 calculation of x[k|k] verified
        P = (I - K * H) * P # PAI 2013-02-12 calculation of P[k|k] verified
        u = (x[0,0] + x[1,0]*1j).conj()
        u /= np.abs(u)
        return (P, x, Q, H, R, I), u

    def demodulate(self, input, (G, uncertainty, var_n)):
        kalman_state = self.kalman_init(uncertainty, var_n)
        pilotPolarity = ofdm.pilotPolarity()
        demapped_bits = []
        i = 0
        taps = 16
        data_history = np.zeros((taps+1, ofdm.Nsc), dtype=complex)
        r = np.zeros((taps+1, ofdm.Nsc), dtype=complex)
        filter = 0.
        while input.size>64:
            sym = np.fft.fftshift(np.fft.fft(input[:64])*G)
            data = sym[ofdm.dataSubcarriers]
            pilots = sym[ofdm.pilotSubcarriers] * pilotPolarity.next() * ofdm.pilotTemplate
            kalman_state, kalman_u = self.kalman_update(kalman_state, np.sum(pilots))
            data *= kalman_u
            pilots *= kalman_u
            if i==0: # signal
                signal_bits = data.real>0
                signal_bits = interleaver.interleave(signal_bits, ofdm.Nsc, 1, reverse=True)
                scrambled_plcp_estimate = code.decode(signal_bits*2-1, 18)
                plcp_estimate = scrambler.scramble(scrambled_plcp_estimate, int(ofdm.Nsc*.5), scramblerState=0)
                parity = (np.sum(plcp_estimate) & 1) == 0
                if not parity:
                    return None, None
                plcp_estimate = util.shiftin(plcp_estimate[:18], 18)[0]
                try:
                    encoding_estimate = util.rev(plcp_estimate & 0xF, 4)
                    rate_estimate = [r.encoding == encoding_estimate for r in self.rates].index(True)
                except ValueError:
                    return None, None
                Nbpsc, constellation_estimate = self.rates[rate_estimate].Nbpsc, self.rates[rate_estimate].constellation
                min_dist = np.diff(np.unique(sorted(constellation_estimate.real)))[0]
                Ncbps = ofdm.Nsc * Nbpsc
                true_rate_estimate = self.rates[rate_estimate].linkRate/12.
                Nbps = int(ofdm.Nsc * true_rate_estimate)
                length_octets_estimate = (plcp_estimate >> 5) & 0xFFF
                length_bits_estimate = length_octets_estimate * 8
                signal_bits = code.encode(scrambled_plcp_estimate)
                dispersion = data - qam.bpsk[1][interleaver.interleave(signal_bits, ofdm.Nsc, 1)]
                dispersion = np.var(dispersion)
            else:
                ll = qam.demapper(data, constellation_estimate, min_dist, dispersion, Nbpsc)
                demapped_bits.append(ll.flatten())
            input = input[80:]
            i += 1
        punctured_bits_estimate = interleaver.interleave(np.concatenate(demapped_bits), Ncbps, Nbpsc, True)
        puncturing_matrix = self.rates[rate_estimate].puncturingMatrix
        coded_bits_estimate = code.depuncture(punctured_bits_estimate, puncturing_matrix)
        target_length = (length_bits_estimate+16+6)*2
        coded_bits_estimate = coded_bits_estimate[:target_length]
        if coded_bits_estimate.size != target_length:
            return None, None
        return coded_bits_estimate, length_bits_estimate

    def decodeFromLLR(self, llr, length_bits):
        scrambled_bits = code.decode(llr, length_bits+16)
        return scrambler.scramble(scrambled_bits, None, scramblerState=0x5d)[:length_bits+16]

    def decode(self, input, lsnr=None):
        score = autocorrelate(np.r_[np.zeros(16), input])[1:]
        #score2 = -score * np.r_[0, np.diff(score, 2), 0]
        #import pdb;pdb.set_trace()
        startIndex = np.max(0, 16*np.argmax(score)-64) #72)
        input = input[startIndex:]
        input, training_data = self.train(input, lsnr if lsnr is not None else 10.)
        llr, length_bits_estimate = self.demodulate(input, training_data)
        if llr is None:
            return None
        output_bits = self.decodeFromLLR(llr, length_bits_estimate)
        if not crc.checkFCS(output_bits[16:]):
            return None
        return util.shiftin(output_bits[16:-32], 8)
