import subprocess
from typing import Optional, Tuple, List
import numpy as np
from tinygrad.helpers import DEBUG, getenv
from tinygrad.ops import Compiled
from tinygrad.runtime.lib import RawBufferCopyInOut, RawMallocBuffer
from tinygrad.codegen.cstyle import CStyleCodegen, CStyleLanguage

ISCUDA = (getenv("CUDACPU", 0) == 0)

from pycuda.compiler import compile as cuda_compile # type: ignore

if ISCUDA:
  import pycuda.autoprimaryctx # type: ignore # pylint: disable=unused-import # noqa: F401
  import pycuda.driver as cuda # type: ignore
  class RawCUDABuffer(RawBufferCopyInOut):
    def __init__(self, size, dtype): super().__init__(size, dtype, cuda.mem_alloc(size * dtype.itemsize))
    def _copyin(self, x:np.ndarray, stream:Optional[cuda.Stream]=None): cuda.memcpy_htod_async(self._buf, x, stream)
    def _copyout(self, x:np.ndarray): cuda.memcpy_dtoh(x, self._buf)
else:
  from extra.cudacpu import ptx_kernel_create, ptx_call

class CUDAProgram:
  def __init__(self, name:str, prg:str, binary=False):
    try:
      if DEBUG >= 6:
        with open("/tmp/cubin", "wb") as f:
          f.write(cuda_compile(prg, target="cubin", no_extern_c=True))
        sass = subprocess.check_output(['nvdisasm', '/tmp/cubin']).decode('utf-8')
        print(sass)
      if not binary or not ISCUDA:
        prg = cuda_compile(prg, target="ptx", no_extern_c=True, arch=(None if ISCUDA else "sm_35"), options=["-Wno-deprecated-gpu-targets"]).decode('utf-8')
    except Exception as e:
      if DEBUG >= 3: print("FAILED TO BUILD", prg)
      raise e
    if DEBUG >= 5: print(prg)
    self.src = prg
    # TODO: name is wrong, so we get it from the ptx using hacks
    if ISCUDA:
      self.prg = cuda.module_from_buffer(prg.encode('utf-8')).get_function(prg.split(".visible .entry ")[1].split("(")[0])
    else:
      self.prg = ptx_kernel_create(prg.encode('utf-8'))

  def __call__(self, global_size:List[int], local_size:List[int], *args, wait=False):
    block_size: Tuple[int,...]  = tuple(local_size + [1] * (3 - len(local_size))) if local_size is not None else (1,1,1)
    grid_size: Tuple[int,...] = tuple(global_size + [1] * (3 - len(global_size)))
    assert all(x%y == 0 for x,y in zip(grid_size, block_size)), f"local:{block_size} must divide global:{grid_size}"
    grid_size = tuple([x//y for x,y in zip(grid_size, block_size)])

    if ISCUDA:
      if wait:
          start, end = cuda.Event(), cuda.Event()
          start.record()
      self.prg(*[x._buf for x in args], block=block_size, grid=grid_size)
      if wait:
        end.record()
        end.synchronize()
        return start.time_till(end)*1e-3
    else:
        ptx_call(self.prg, args, block_size, grid_size)

class CUDACodegen(CStyleCodegen):
  lang = CStyleLanguage(
    kernel_prefix = "__global__", smem_prefix = "__shared__ ", barrier = "__syncthreads();", float4 = "make_float4",
    half_prekernel = "#include <cuda_fp16.h>",
    gid = [f'blockDim.{chr(120+i)}*blockIdx.{chr(120+i)}+threadIdx.{chr(120+i)}' for i in range(3)],
    lid = [f'threadIdx.{chr(120+i)}' for i in range(3)])
  supports_float4_alu = False

if ISCUDA:
  CUDABuffer = Compiled(RawCUDABuffer, CUDACodegen, CUDAProgram, cuda.Context.synchronize)
else:
  CUDABuffer = Compiled(RawMallocBuffer, CUDACodegen, CUDAProgram)
