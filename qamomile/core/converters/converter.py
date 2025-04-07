"""
qamomile/core/converter.py

This module defines the QuantumConverter abstract base class for converting between
different problem representations and quantum models in Qamomile.

The QuantumConverter class provides a framework for encoding classical optimization
problems into quantum representations (e.g., Ising models) and decoding quantum
computation results back into classical problem solutions.

Key Features:

- Conversion between classical optimization problems and Ising models.

- Abstract methods for generating cost Hamiltonians and decoding results.

- Integration with QuantumSDKTranspiler for SDK-specific result handling.

Usage:

Developers implementing specific quantum conversion strategies should subclass
QuantumConverter and implement the abstract methods. The class is designed to work
with jijmodeling for problem representation and various quantum SDKs through
the QuantumSDKTranspiler interface.

Example:
    .. code::

        class QAOAConverter(QuantumConverter):
            def get_cost_hamiltonian(self) -> qm_o.Hamiltonian:
                # Implementation for generating QAOA cost Hamiltonian
                ...

"""

import abc
import enum
import typing as typ

import jijmodeling as jm
import ommx.v1
import numpy as np
import qamomile.core.bitssample as qm_bs
import qamomile.core.operator as qm_o
from qamomile.core.ising_qubo import IsingModel, qubo_to_ising
from qamomile.core.transpiler import QuantumSDKTranspiler

ResultType = typ.TypeVar("ResultType")


class RelaxationMethod(enum.Enum):
    """
    Enumeration for relaxation methods used in quantum problem conversion.

    Attributes:
        AugmentedLagrangian: Augmented Lagrangian method for PUBO conversion.
        Penalty: Penalty method for PUBO conversion.
        None: No relaxation method applied.
    """
    AugmentedLagrangian = "AugmentedLagrangian"
    SquaredPenalty = "SquaredPenalty"


class QuantumConverter(abc.ABC):
    """
    Abstract base class for quantum problem converters in Qamomile.

    This class provides methods for encoding classical optimization problems
    into quantum representations (e.g., Ising models) and decoding quantum
    computation results back into classical problem solutions.

    Attributes:
        compiled_instance: The compiled instance of the optimization problem.
        pubo_builder: The PUBO (Polynomial Unconstrained Binary Optimization) builder.
        _ising (Optional[IsingModel]): Cached Ising model representation.

    Methods:
        get_ising: Retrieve or compute the Ising model representation.
        ising_encode: Encode the problem into an Ising model.
        get_cost_hamiltonian: Abstract method to get the cost Hamiltonian.
        decode: Decode quantum computation results into a SampleSet.
        decode_bits_to_sampleset: Abstract method to convert BitsSampleSet to SampleSet.
    """

    def __init__(
        self,
        instance: ommx.v1.Instance,
        relax_method: RelaxationMethod = RelaxationMethod.SquaredPenalty,
        normalize_model: bool = False,
        normalize_ising: typ.Optional[typ.Literal["abs_max", "rms"]] = None,
    ):
        """
        Initialize the QuantumConverter.

        This method initializes the converter with the compiled instance of the optimization problem

        Args:
            compiled_instance: ommx.v1.Instance.
            relax_method (RelaxationMethod): The relaxation method for PUBO conversion.
                Defaults to AugmentedLagrangian.
            normalize_model (bool): The objective function and the constraints are normalized using the maximum absolute value of the coefficients contained in each.
                Defaults to False
            normalize_ising (Literal["abs_max", "rms"] | None): The normalization method for the Ising Hamiltonian. 
                Available options:
                - "abs_max": Normalize by absolute maximum value
                - "rms": Normalize by root mean square
                Defaults to None.

        """

        self.original_instance: ommx.v1.Instance = instance

        self.int2varlabel: dict[int, str] = {}
        self.normalize_ising = normalize_ising

        self._ising: typ.Optional[IsingModel] = None

    def instance_to_qubo(self) -> tuple[dict[tuple[int, int], float], float]:
        """
        Convert the instance to QUBO format.

        This method converts the optimization problem instance into a QUBO (Quadratic Unconstrained Binary Optimization)
        representation, which is suitable for quantum computation.

        Returns:
            tuple[dict[int, float], float]: A tuple containing the QUBO dictionary and the constant term.

        """
        qubo, constant = self.original_instance.to_qubo()
        return qubo, constant

    def get_ising(self) -> IsingModel:
        """
        Get the Ising model representation of the problem.

        Returns:
            IsingModel: The Ising model representation.

        """
        if self._ising is None:
            self._ising = self.ising_encode()
        return self._ising

    def ising_encode(
        self,
        multipliers: typ.Optional[dict[str, float]] = None,
        detail_parameters: typ.Optional[
            dict[str, dict[tuple[int, ...], tuple[float, float]]]
        ] = None,
    ) -> IsingModel:
        """
        Encode the problem to an Ising model.

        This method converts the problem from QUBO (Quadratic Unconstrained Binary Optimization)
        to Ising model representation.

        Args:
            multipliers (Optional[dict[str, float]]): Multipliers for constraint terms.
            detail_parameters (Optional[dict[str, dict[tuple[int, ...], tuple[float, float]]]]):
                Detailed parameters for the encoding process.

        Returns:
            IsingModel: The encoded Ising model.

        """

        qubo, constant = self.instance_to_qubo()
        # TODO: When simplify-True, we met some errors.
        #       Need to be fixed.
        ising = qubo_to_ising(qubo, simplify=False)
        ising.constant += constant

        if isinstance(self.normalize_ising, str):
            if self.normalize_ising == "abs_max":
                ising.normalize_by_abs_max()
            elif self.normalize_ising == "rms":
                ising.normalize_by_rms()
            else:
                raise ValueError(
                    f"Invalid value for normalize_ising: {self.normalize_ising}"
                )

        deci_vars = {dv.id: dv for dv in self.original_instance.raw.decision_variables}
        if ising.index_map is not None:
            for ising_index, qubo_index in ising.index_map.items():
                deci_var = deci_vars[qubo_index]
                # TODO: If use log encoding to represent an integer,
                #       var_name is ommx.log_encode and subscripts represents [original variable index, encoded binary index].
                #       Need to be fixed.
                var_name = deci_var.name
                subscripts = deci_var.subscripts
                self.int2varlabel[ising_index]  = var_name + "_{" + ",".join(map(str, subscripts)) + "}"

        return ising

    @abc.abstractmethod
    def get_cost_hamiltonian(self) -> qm_o.Hamiltonian:
        """
        Abstract method to get the cost Hamiltonian for the quantum problem.

        This method should be implemented in subclasses to define how the
        cost Hamiltonian is constructed for specific quantum algorithms.

        Returns:
            qm_o.Hamiltonian: The cost Hamiltonian for the quantum problem.

        Raises:
            NotImplementedError: If the method is not implemented in the subclass.
        """
        raise NotImplementedError()

    def decode(
        self, transpiler: QuantumSDKTranspiler[ResultType], result: ResultType
    ) -> ommx.v1.SampleSet:
        """
        Decode quantum computation results into a SampleSet.

        This method uses the provided transpiler to convert SDK-specific results
        into a BitsSampleSet, then calls decode_bits_to_sampleset to produce
        the final SampleSet.

        Args:
            transpiler (QuantumSDKTranspiler[ResultType]): The transpiler for the specific quantum SDK.
            result (ResultType): The raw result from the quantum computation.

        Returns:
            jm.experimental.SampleSet: The decoded results as a SampleSet.
        """
        bitssampleset = transpiler.convert_result(result)
        return self.decode_bits_to_sampleset(bitssampleset)

    def decode_bits_to_sampleset(
        self, bitssampleset: qm_bs.BitsSampleSet
    ) -> ommx.v1.SampleSet:
        """
        Decode a BitArraySet to a SampleSet.

        This method converts the quantum computation results (bitstrings)
        into a format that represents solutions to the original optimization problem.

        Args:
            bitarray_set (qm_c.BitArraySet): The set of bitstring results from quantum computation.

        Returns:
            ommx.v1.SampleSet: The decoded results as a SampleSet.
        """
        ising = self.get_ising()

        # Create ommx.v1.Samples
        sample_id = 0
        entries = []
        for bitssample in bitssampleset.bitarrays:
            sample = {}
            for i, bit in enumerate(bitssample.bits):
                index = ising.ising2qubo_index(i)
                sample[index] = bit
            state = ommx.v1.State(entries=sample)
            # `num_occurrences` is encoded into sample ID list.
            # For example, if `num_occurrences` is 2, there are two samples with the same state, thus two sample IDs are generated.
            ids = []
            for _ in range(bitssample.num_occurrences):
                ids.append(sample_id)
                sample_id += 1
            entries.append(ommx.v1.Samples.SamplesEntry(state=state, ids=ids))

        samples = ommx.v1.Samples(entries=entries)

        return self.original_instance.evaluate_samples(samples)


