from llvm_cbuilder import *
from llvm_cbuilder import shortnames as C
from llvm.core import *
from llvm.passes import *
import numpy as np
from numba import llvm_types
from . import _common
from ._common import _llvm_ty_to_numpy

from numba.minivect import minitypes

from numbapro._cuda.error import CudaSupportError
from numbapro._cuda import nvvm
from numbapro import cuda
try:
    from numbapro import _cudadispatch
except CudaSupportError: # ignore missing cuda dependency
    pass

from numbapro.translate import Translate
from numbapro.vectorize import minivectorize, basic
from numba.ndarray_helpers import PyArrayAccessor

class _CudaStagingCaller(CDefinition):
    def body(self, *args, **kwargs):
        worker = self.depends(self.WorkerDef)
        inputs = args[:-2]
        output, ct = args[-2:]

        # get current thread index
        fty_sreg = Type.function(Type.int(), [])
        def get_ptx_sreg(name):
            m = self.function.module
            prefix = 'llvm.nvvm.read.ptx.sreg.'
            return CFunc(self, m.get_or_insert_function(fty_sreg,
                                                        name=prefix + name))

        tid_x = get_ptx_sreg('tid.x')
        ntid_x = get_ptx_sreg('ntid.x')
        ctaid_x = get_ptx_sreg('ctaid.x')

        tid = self.var_copy(tid_x())
        blkdim = self.var_copy(ntid_x())
        blkid = self.var_copy(ctaid_x())

        i = tid + blkdim * blkid

        with self.ifelse( i >= ct ) as ifelse: # stop condition
            with ifelse.then():
                self.ret()

        dataptrs = []
        for inary, ty in zip(inputs, self.ArgTypes):
            acc = PyArrayAccessor(self.builder, inary.value)
            casted_data = self.builder.bitcast(acc.data, ty)
            dataptrs.append(CArray(self, casted_data))

        res = worker(*[x[i] for x in dataptrs], inline=True)

        acc = PyArrayAccessor(self.builder, output.value)
        casted_data = self.builder.bitcast(acc.data, self.RetType)
        outary = CArray(self, casted_data)
        outary[i] = res

        self.ret()

    @classmethod
    def specialize(cls, worker, fntype):
        cls._name_ = ("_cukernel_%s" % worker).replace('.', ('_%X_' % ord('.')))
        
        args = cls._argtys_ = []
        inargs = cls.InArgs = []
        cls.ArgTypes = map(cls._pointer, fntype.args)
        cls.RetType = cls._pointer(fntype.return_type)

        # input arguments
        for i, ty in enumerate(fntype.args):
            cur = ('in%d' % i, cls._pointer(llvm_types._numpy_struct))
            args.append(cur)
            inargs.append(cur)

        # output arguments
        cur = ('out', cls._pointer(llvm_types._numpy_struct))
        args.append(cur)
        cls.OutArg = cur

        # extra arguments
        args.append(('ct', C.int))

        cls.WorkerDef = worker

    @classmethod
    def _pointer(cls, ty):
        return C.pointer(ty)

def get_dtypes(restype, argtypes):
    try:
        ret_dtype = minitypes.map_minitype_to_dtype(restype)
    except KeyError:
        ret_dtype = None

    arg_dtypes = tuple(minitypes.map_minitype_to_dtype(arg_type)
                           for arg_type in argtypes)
    return ret_dtype, arg_dtypes

class CudaVectorize(_common.GenericVectorize):
    def __init__(self, func):
        super(CudaVectorize, self).__init__(func)
        self.module = Module.new('ptx_%s' % func)
        self.signatures = []

    def add(self, restype, argtypes, **kwargs):
        self.signatures.append((restype, argtypes, kwargs))
        translate = cuda._ast_jit(self.pyfunc, argtypes, inline=False,
                                  llvm_module=self.module, **kwargs)
        self.translates.append(translate)
        self.args_restypes.append(argtypes + [restype])

    def _get_lfunc_list(self):
        return [lfunc for sig, lfunc in self.translates]

    def _get_sig_list(self):
        return [sig for sig, lfunc in self.translates]

    def _get_ee(self):
        raise NotImplementedError

    def build_ufunc(self):
        lfunclist = self._get_lfunc_list()
        signatures = self._get_sig_list()
        types_to_retty_kernel = {}
        
        for (restype, argtypes, _), lfunc, sig in zip(self.signatures,
                                                      lfunclist,
                                                      signatures):
            ret_dtype, arg_dtypes = get_dtypes(restype, argtypes)
            # generate a caller for all functions
            cukernel = self._build_caller(lfunc)
            assert cukernel.module is lfunc.module


            # unicode problem?
            fname = cukernel.name
            if isinstance(fname, unicode):
                fname = fname.encode('utf-8')

            cuf = cuda.CudaNumbaFunction(self.pyfunc,
                                         signature=sig,
                                         lfunc=cukernel)

            types_to_retty_kernel[arg_dtypes] = ret_dtype, cuf

        for lfunc in lfunclist:
            lfunc.delete()

        dispatcher = _cudadispatch.CudaUFuncDispatcher(types_to_retty_kernel)
        return dispatcher

    def build_ufunc_core(self):
        # TODO: implement this after the refactoring of cuda dispatcher
        raise NotImplementedError

    def _build_caller(self, lfunc):
        lcaller_def = _CudaStagingCaller(CFuncRef(lfunc), lfunc.type.pointee)
        lcaller = lcaller_def(self.module)
        return lcaller
