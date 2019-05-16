import pyevmasm

from ..annotation import modifies_stack
from ..std import stack_manip
from ..std import byterange
from ..vm import AVMOp
from ..ast import AVMLabel
from ..ast import BlockStatement
from ..compiler import compile_block
from .. import value

from . import os
from . import execution

# [val]
# def replace_single_int_sha3(instrs):
#     out = []
#     i = 0
#     while i < len(instrs):
#         if (
#                 instrs[i].name == "PUSH1" and instrs[i].operand == 0
#                 and instrs[i + 1].name == "MSTORE"
#                 and instrs[i + 2].name == "PUSH1" and instrs[i + 2].operand == 32
#                 and instrs[i + 3].name == "PUSH1" and instrs[i + 3].operand == 0
#                 and instrs[i + 4].name == "SHA3"
#         ):
#             out.append(AVMOp("HASH"))
#             i += 5
#         else:
#             out.append(instrs[i])
#             i += 1
#     return out


def replace_self_balance(instrs):
    out = []
    i = 0
    while i < len(instrs):
        if (
                instrs[i].name == "ADDRESS"
                and instrs[i + 1].name == "PUSH20"
                and instrs[i + 1].operand == (2**160 - 1)
                and instrs[i + 2].name == "AND"
                and instrs[i + 3].name == "BALANCE"
        ):
            out.append(AVMOp("SELF_BALANCE"))
            i += 4
        else:
            out.append(instrs[i])
            i += 1
    return out


def remove_metadata(instrs):
    for i in range(len(instrs) - 2, -1, -1):
        first_byte = instrs[i].opcode
        second_byte = instrs[i + 1].opcode
        if first_byte == 0xa1 and second_byte == 0x65:
            return instrs[:i]
    return instrs


def make_bst_lookup(items):
    def handle_bad_jump(vm):
        vm.tnewn(0)

    if len(items) >= 3:
        mid = len(items) // 2
        left = items[:mid]
        right = items[mid + 1:]
        pivot = items[mid][0]

        def impl_n(vm):
            # index
            vm.push(pivot)
            vm.dup1()
            vm.lt()
            # index < pivot, index
            vm.ifelse(lambda vm: [
                # index < pivot
                make_bst_lookup(left)(vm)
            ], lambda vm: [
                vm.push(pivot),
                vm.dup1(),
                vm.gt(),
                vm.ifelse(lambda vm: [
                    # index > pivot
                    make_bst_lookup(right)(vm)
                ], lambda vm: [
                    # index == pivot
                    vm.pop(),
                    vm.push(items[mid][1])
                ])
            ])
        return impl_n

    if len(items) == 2:
        def impl_2(vm):
            # index
            vm.dup0()
            vm.push(items[0][0])
            vm.eq()
            vm.ifelse(lambda vm: [
                vm.pop(),
                vm.push(items[0][1])
            ], lambda vm: [
                vm.push(items[1][0]),
                vm.eq(),
                vm.ifelse(lambda vm: [
                    vm.push(items[1][1])
                ], handle_bad_jump)
            ])
        return impl_2

    if len(items) == 1:
        def impl_1(vm):
            vm.push(items[0][0])
            vm.eq()
            vm.ifelse(lambda vm: [
                vm.push(items[0][1])
            ], handle_bad_jump)
        return impl_1

    return handle_bad_jump


def generate_evm_code(raw_code, storage):
    contract_dispatch = []
    impls = []
    contract_info = []

    contracts = {}
    code_tuples = {}
    for contract in raw_code:
        code_tuples[contract] = byterange.frombytes(
            bytes.fromhex(raw_code[contract].hex())
        )
        contracts[contract] = list(pyevmasm.disassemble_all(
            raw_code[contract]
        ))

    code_sizes = []
    for contract in sorted(contracts):
        code_sizes.append((contract, len(contracts[contract])))
        contract_dispatch.append((
            contract,
            AVMLabel("contract_entry_" + str(contract))
        ))

    contract_dispatch_impl = make_bst_lookup(contract_dispatch)

    @modifies_stack([value.IntType()], 1)
    def dispatch_contract(vm):
        contract_dispatch_impl(vm)

    code_size_func = make_bst_lookup(code_sizes)

    @modifies_stack([value.IntType()], 1)
    def code_size(vm):
        code_size_func(vm)

    for contract in sorted(contracts):
        if contract not in storage:
            storage[contract] = {}
        code = contracts[contract]
        label = AVMLabel("contract_entry_" + str(contract))
        code = generate_contract_code(
            label,
            code,
            code_tuples[contract],
            contract,
            code_size,
            dispatch_contract
        )
        impls.append(code)
        contract_dispatch.append((contract, label))
        contract_info.append({
            "contractID": contract,
            "storage": storage[contract]
        })

    def initialization(vm):
        os.initialize(vm, contract_info)
        vm.jump_direct(AVMLabel("run_loop_start"))

    def run_loop_start(vm):
        vm.set_label(AVMLabel("run_loop_start"))
        os.get_next_message(vm)
        execution.setup_initial_call(vm, dispatch_contract)
        vm.push(AVMLabel("run_loop_start"))
        vm.jump()

    main_code = []
    main_code.append(compile_block(run_loop_start))
    main_code += impls
    return compile_block(initialization), BlockStatement(main_code)


def generate_contract_code(label, code, code_tuple, contract_id, code_size, dispatch_contract):
    code = remove_metadata(code)
    code = replace_self_balance(code)

    jump_table = []
    for insn in code:
        if insn.name == "JUMPDEST":
            jump_table.append((
                insn.pc,
                AVMLabel(f"jumpdest_{contract_id}_{insn.pc}")
            ))

    jump_table = sorted(jump_table, key=lambda x: x[0])
    dispatch_func = make_bst_lookup(jump_table)

    @modifies_stack([value.IntType()], [value.ValueType()], contract_id)
    def dispatch(vm):
        dispatch_func(vm)

    @modifies_stack(0, 1, contract_id)
    def get_contract_code(vm):
        vm.push(code_tuple)

    def run_op(instr):
        def impl(vm):
            if instr.name == "HASH":
                vm.hash()
            elif instr.name == "SELF_BALANCE":
                vm.push(0)
                os.balance_get(vm)
            elif instr.name == "ADD":
                vm.add()
            elif instr.name == "MUL":
                vm.mul()
            elif instr.name == "SUB":
                vm.sub()
            elif instr.name == "DIV":
                vm.div()
            elif instr.name == "MOD":
                vm.mod()
            elif instr.name == "EXP":
                vm.exp()
            elif instr.name == "AND":
                vm.bitwise_and()
            elif instr.name == "OR":
                vm.bitwise_or()
            elif instr.name == "NOT":
                vm.bitwise_not()
            elif instr.name == "LT":
                vm.lt()
            elif instr.name == "GT":
                vm.gt()
            elif instr.name == "EQ":
                vm.eq()
            elif instr.name == "ISZERO":
                vm.iszero()
            elif instr.name == "TIMESTAMP":
                os.message_timestamp(vm)
            elif instr.name[:4] == "SWAP":
                swap_num = int(instr.name[4:])
                if swap_num == 1:
                    vm.swap1()
                elif swap_num == 2:
                    vm.swap2()
                else:
                    stack_manip.swap_n(swap_num)(vm)
            elif instr.name == "POP":
                vm.pop()
            elif instr.name == "SLOAD":
                os.storage_load(vm)
            elif instr.name == "SSTORE":
                os.storage_store(vm)
            elif instr.name == "MSTORE":
                os.memory_store(vm)
            elif instr.name == "MLOAD":
                os.memory_load(vm)
            elif instr.name == "ADDRESS":
                vm.push(contract_id)
            elif instr.name == "CALLER":
                os.message_caller(vm)
            elif instr.name == "CALLVALUE":
                os.message_value(vm)
            elif instr.name == "CALLDATALOAD":
                os.message_data_load(vm)
            elif instr.name == "CALLDATASIZE":
                os.message_data_size(vm)
            elif instr.name == "CODESIZE":
                vm.push(len(code))
            elif instr.name == "EXTCODESIZE":
                code_size(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.halt()
                ])
            elif instr.name == "GAS":
                vm.push(9999999999)
            elif instr.name == "CODECOPY":
                os.evm_copy_to_memory(vm, get_contract_code)
            elif instr.name[:4] == "PUSH":
                vm.push(instr.operand)
            elif instr.name[:3] == "DUP":
                dup_num = int(instr.name[3:]) - 1
                if dup_num == 0:
                    vm.dup0()
                elif dup_num == 1:
                    vm.dup1()
                elif dup_num == 2:
                    vm.dup2()
                else:
                    stack_manip.dup_n(dup_num)(vm)
            elif instr.name == "RETURNDATASIZE":
                os.return_data_size(vm)
            elif instr.name == "RETURNDATACOPY":
                os.return_data_copy(vm)
            elif instr.name == "CALL":
                execution.call(vm, dispatch_contract, instr.pc, contract_id)
            elif instr.name == "STATICCALL":
                execution.staticcall(vm, dispatch_contract, instr.pc, contract_id)
            elif instr.name == "STOP":
                execution.stop(vm)
            elif instr.name == "REVERT":
                execution.revert(vm)
            elif instr.name == "RETURN":
                execution.ret(vm)
            elif instr.name == "INVALID":
                execution.revert(vm)
            elif instr.name == "SELFDESTRUCT":
                execution.selfdestruct(vm)
            elif instr.name == "JUMPI":
                dispatch(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.halt()
                ], lambda vm: [
                    vm.cjump()
                ])
            elif instr.name == "JUMP":
                dispatch(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.halt()
                ], lambda vm: [
                    vm.jump()
                ])
            elif instr.name == "JUMPDEST":
                vm.set_label(AVMLabel(f"jumpdest_{contract_id}_{instr.pc}"))
            elif instr.name == "SHA3":
                os.evm_sha3(vm)
            elif instr.name == "LOG1":
                os.evm_log1(vm)
            elif instr.name == "LOG2":
                os.evm_log2(vm)
            elif instr.name == "LOG3":
                os.evm_log3(vm)
            else:
                raise Exception(f"Unhandled instruction {instr}")
        return impl

    contract_code = [label]
    for insn in code:
        block = compile_block(run_op(insn))
        block.add_node(f"EthOp({insn}, {insn.pc})")
        contract_code.append(block)

    return BlockStatement(contract_code)
