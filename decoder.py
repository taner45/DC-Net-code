from genericpath import isfile
import numpy as np
import pathlib
import torch
from torch import nn
from torch.nn import functional as F
from geoist import gridder
from geoist.pfm import prism
import geoist.inversion.toeplitz as tptz
from geoist.inversion.mesh import PrismMesh
from pathos.multiprocessing import Pool
import time
import os
class GravDecoder(nn.Module):
    '''Our density model is devided into cubics.
    Args:
        dxyz (tuple of numbers): dimension of each cubic in meters.

    Attributes:
        dxyz (tuple of numbers): dimension of each cubic in meters.
        nxyz (tuple of int): number of cell along each axis.
        G_const (double): Gravity constant in SI unit. 
        cell_volume (double): Volume of each cell in SI unit.
        GV (double): Product of cell volume and Gravity constant.
        kernel_eigs (Tensor): eigs of kernel matrix.
    '''
    def __init__(self, dzyx=(50.,100.,100.),nzyx=(32,64,64),data_dir='./models'):
        super(GravDecoder, self).__init__()
        self._name = 'GravDecoder'
        self.dzyx = dzyx
        self.nzyx = nzyx 
        self.G_const = 6.674e-11
        self.cell_volume = np.prod(self.dzyx)
        self.GV = self.G_const * self.cell_volume
        self.data_dir = data_dir
        self.gen_kernel_eigs()

    def forward(self,data_input):
        '''Calculate gravity field from density model
        Args:
        data_input (Tensor): density model, with shape (nbatch,nz,ny,nx)

        Return:
        res (Tensor): field of each layer generated by the input density model, with shape
                      (nbatch,nz,ny,nx)
        '''
        final_res = torch.zeros_like(data_input)
        for ilayer in range(data_input.shape[1]):
            res = torch.zeros((data_input.shape[0],
                               2*self.nzyx[1],
                               2*self.nzyx[2],
                               2),device=self.kernel_eigs.device)
            expand_v = torch.zeros((data_input.shape[0],
                                    2*data_input.shape[2],
                                    2*data_input.shape[3],
                                    2),
                                   device=self.kernel_eigs.device,
                                   dtype=torch.double)
            expand_v[:,:self.nzyx[1],:self.nzyx[2],0] = data_input[:,ilayer,:,:]
            eigs_layer_i = expand_v.fft(2,normalized=True)
            
            tmp_0 = (eigs_layer_i[:,:,:,0] * self.kernel_eigs[ilayer,:,:,0]
                     -eigs_layer_i[:,:,:,1] * self.kernel_eigs[ilayer,:,:,1])
            tmp_1 = (eigs_layer_i[:,:,:,0] * self.kernel_eigs[ilayer,:,:,1]
                     +eigs_layer_i[:,:,:,1] * self.kernel_eigs[ilayer,:,:,0])
            res[:,:,:,0] = tmp_0
            res[:,:,:,1] = tmp_1
            res = res.ifft(2,normalized=True)
            #res = torch.fft.irfft2(res)  # vim20220426 改
            
            #res = torch.stack((res.real, res.imag), -1)
              
            shape = res.shape
            final_res[:,ilayer,:,:] = res[:,:shape[1]//2,:shape[2]//2,0]
        return final_res

    def gen_kernel_eigs(self):
        fname = '{}x{}x{}_{:.0f}x{:.0f}x{:.0f}_lbl.pt'.format(*self.nzyx,*self.dzyx)
        #fname = pathlib.Path(pathlib.Path(self.data_dir)/pathlib.Path(fname))
        fname = pathlib.Path(pathlib.Path(self.data_dir)/pathlib.Path(fname))
        if fname.is_file():
            kernel_eigs = torch.load(fname)
        else:
            density = np.ones((self.nzyx[1],self.nzyx[2]))*1.0e3
            kernel_eigs = torch.empty(self.nzyx[0],2*self.nzyx[1],2*self.nzyx[2],2)
            for i in range(self.nzyx[0]): #垂向分层
                # first generate geometries
                source_volume = [-self.nzyx[2]*self.dzyx[2]/2,
                                 self.nzyx[2]*self.dzyx[2]/2,
                                 -self.nzyx[1]*self.dzyx[1]/2,
                                 self.nzyx[1]*self.dzyx[1]/2,
                                 20+self.dzyx[0]*i,
                                 20+self.dzyx[0]*(i+1)]
                mesh = PrismMesh(source_volume,(1,self.nzyx[1],self.nzyx[2]))
                mesh.addprop('density',density.ravel())
                obs_area = (source_volume[0]+0.5*self.dzyx[2],
                            source_volume[1]-0.5*self.dzyx[2],
                            source_volume[2]+0.5*self.dzyx[1],
                            source_volume[3]-0.5*self.dzyx[1])
                obs_shape = (self.nzyx[2],self.nzyx[1])
                xp,yp,zp = gridder.regular(obs_area,obs_shape,z=-1)
                # then generate the kernel matrix operator.
                def calc_kernel(i):
                    return prism.gz(xp[0:1],yp[0:1],zp[0:1],[mesh[i]])
                with Pool(processes=16) as pool:
                    kernel0 = pool.map(calc_kernel,range(len(mesh)))
                kernel0 = np.array(kernel0).reshape(1,self.nzyx[1],self.nzyx[2])
                kernel_op = tptz.GToepOperator(kernel0)
                kernel_eigs[i,:,:,0] = torch.as_tensor(kernel_op.eigs[0].real,dtype=torch.float32)
                kernel_eigs[i,:,:,1] = torch.as_tensor(kernel_op.eigs[0].imag,dtype=torch.float32)
            torch.save(kernel_eigs,fname)
        self.register_buffer('kernel_eigs',kernel_eigs)