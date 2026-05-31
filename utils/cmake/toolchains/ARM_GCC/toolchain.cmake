find_program(ARM_NONE_EABI_RANLIB arm-none-eabi-ranlib)
find_program(ARM_NONE_EABI_AR arm-none-eabi-ar)
find_program(ARM_NONE_EABI_GCC arm-none-eabi-gcc arm-none-eabi-gcc-15.2.1)
find_program(ARM_NONE_EABI_GPP arm-none-eabi-g++)
find_program(ARM_NONE_EABI_OBJCOPY arm-none-eabi-objcopy)
find_program(ARM_NONE_EABI_SIZE arm-none-eabi-size)

function(_codal_arm_gcc_has_stdint _gcc_path _result_var _include_dir_var)
    if(NOT _gcc_path)
        set(${_result_var} FALSE PARENT_SCOPE)
        set(${_include_dir_var} "" PARENT_SCOPE)
        return()
    endif()

    execute_process(
        COMMAND ${_gcc_path} -print-sysroot
        OUTPUT_VARIABLE _gcc_sysroot
        OUTPUT_STRIP_TRAILING_WHITESPACE
    )

    set(_candidate_include_dir "${_gcc_sysroot}/include")

    if(EXISTS "${_candidate_include_dir}/stdint.h")
        set(${_result_var} TRUE PARENT_SCOPE)
    else()
        set(${_result_var} FALSE PARENT_SCOPE)
    endif()

    set(${_include_dir_var} "${_candidate_include_dir}" PARENT_SCOPE)
endfunction()

set(_ARM_GCC_TOOLCHAIN_MISSING "")

# Prefer complete Arm GNU Toolchain bundles on macOS if available.
file(GLOB _ARM_GNU_TOOLCHAIN_GCC_CANDIDATES "/Applications/ArmGNUToolchain/*/arm-none-eabi/bin/arm-none-eabi-gcc")
list(SORT _ARM_GNU_TOOLCHAIN_GCC_CANDIDATES ORDER DESCENDING)

if(NOT ARM_NONE_EABI_GCC)
    foreach(_candidate_gcc ${_ARM_GNU_TOOLCHAIN_GCC_CANDIDATES})
        get_filename_component(_candidate_bindir "${_candidate_gcc}" DIRECTORY)
        if(EXISTS "${_candidate_bindir}/arm-none-eabi-g++" AND
           EXISTS "${_candidate_bindir}/arm-none-eabi-ar" AND
           EXISTS "${_candidate_bindir}/arm-none-eabi-ranlib" AND
           EXISTS "${_candidate_bindir}/arm-none-eabi-objcopy" AND
           EXISTS "${_candidate_bindir}/arm-none-eabi-size")
            set(ARM_NONE_EABI_GCC "${_candidate_gcc}")
            set(ARM_NONE_EABI_GPP "${_candidate_bindir}/arm-none-eabi-g++")
            set(ARM_NONE_EABI_AR "${_candidate_bindir}/arm-none-eabi-ar")
            set(ARM_NONE_EABI_RANLIB "${_candidate_bindir}/arm-none-eabi-ranlib")
            set(ARM_NONE_EABI_OBJCOPY "${_candidate_bindir}/arm-none-eabi-objcopy")
            set(ARM_NONE_EABI_SIZE "${_candidate_bindir}/arm-none-eabi-size")
            message(STATUS "Using ARM GCC from ${_candidate_gcc}")
            break()
        endif()
    endforeach()
endif()

foreach(_tool ARM_NONE_EABI_GCC ARM_NONE_EABI_GPP ARM_NONE_EABI_AR ARM_NONE_EABI_RANLIB ARM_NONE_EABI_OBJCOPY ARM_NONE_EABI_SIZE)
    if(NOT ${_tool})
        list(APPEND _ARM_GCC_TOOLCHAIN_MISSING ${_tool})
    endif()
endforeach()

if(_ARM_GCC_TOOLCHAIN_MISSING)
    message(FATAL_ERROR
        "ARM GNU Embedded toolchain is required but not found in PATH.\n"
        "Missing: ${_ARM_GCC_TOOLCHAIN_MISSING}\n"
        "Install and ensure binaries are on PATH.\n"
        "macOS: brew install --cask gcc-arm-embedded\n"
        "Ubuntu/Debian: sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi")
endif()

# Validate that the compiler has an ARM C library sysroot installed.
# Some package managers ship GCC binaries without target libc headers.
_codal_arm_gcc_has_stdint("${ARM_NONE_EABI_GCC}" ARM_NONE_EABI_HAS_STDINT ARM_NONE_EABI_CANDIDATE_INCLUDE_DIR)

if(NOT ARM_NONE_EABI_HAS_STDINT)
    foreach(_candidate_gcc ${_ARM_GNU_TOOLCHAIN_GCC_CANDIDATES})
        _codal_arm_gcc_has_stdint("${_candidate_gcc}" _candidate_has_stdint _candidate_include_dir)
        if(_candidate_has_stdint)
            set(ARM_NONE_EABI_GCC "${_candidate_gcc}")
            get_filename_component(_candidate_bindir "${_candidate_gcc}" DIRECTORY)
            if(EXISTS "${_candidate_bindir}/arm-none-eabi-g++")
                set(ARM_NONE_EABI_GPP "${_candidate_bindir}/arm-none-eabi-g++")
            endif()
            if(EXISTS "${_candidate_bindir}/arm-none-eabi-ar")
                set(ARM_NONE_EABI_AR "${_candidate_bindir}/arm-none-eabi-ar")
            endif()
            if(EXISTS "${_candidate_bindir}/arm-none-eabi-ranlib")
                set(ARM_NONE_EABI_RANLIB "${_candidate_bindir}/arm-none-eabi-ranlib")
            endif()
            if(EXISTS "${_candidate_bindir}/arm-none-eabi-objcopy")
                set(ARM_NONE_EABI_OBJCOPY "${_candidate_bindir}/arm-none-eabi-objcopy")
            endif()
            if(EXISTS "${_candidate_bindir}/arm-none-eabi-size")
                set(ARM_NONE_EABI_SIZE "${_candidate_bindir}/arm-none-eabi-size")
            endif()

            set(ARM_NONE_EABI_HAS_STDINT TRUE)
            set(ARM_NONE_EABI_CANDIDATE_INCLUDE_DIR "${_candidate_include_dir}")
            message(STATUS "Using ARM GCC from ${_candidate_gcc}")
            break()
        endif()
    endforeach()
endif()

if(NOT EXISTS "${ARM_NONE_EABI_CANDIDATE_INCLUDE_DIR}/stdint.h")
    message(FATAL_ERROR
        "arm-none-eabi-gcc was found, but target C library headers are missing (cannot locate stdint.h in ${ARM_NONE_EABI_CANDIDATE_INCLUDE_DIR}).\n"
        "A complete ARM embedded toolchain (GCC + newlib/libc sysroot) is required.\n"
        "On macOS, install the Arm GNU toolchain bundle (brew install --cask gcc-arm-embedded).\n"
        "Then ensure arm-none-eabi-gcc resolves to the complete toolchain binary.")
endif()

set(CMAKE_OSX_SYSROOT "/")
set(CMAKE_OSX_DEPLOYMENT_TARGET "")

set(CMAKE_SYSTEM_NAME "Generic")
set(CMAKE_SYSTEM_VERSION "2.0.0")

set(CODAL_TOOLCHAIN "ARM_GCC")

if(CMAKE_VERSION VERSION_LESS "3.5.0")
    include(CMakeForceCompiler)
    cmake_force_c_compiler("${ARM_NONE_EABI_GCC}" GNU)
    cmake_force_cxx_compiler("${ARM_NONE_EABI_GPP}" GNU)
else()
    # from 3.5 the force_compiler macro is deprecated: CMake can detect
    # arm-none-eabi-gcc as being a GNU compiler automatically
    set(CMAKE_TRY_COMPILE_TARGET_TYPE "STATIC_LIBRARY")
    set(CMAKE_C_COMPILER "${ARM_NONE_EABI_GCC}" CACHE FILEPATH "C compiler" FORCE)
    set(CMAKE_CXX_COMPILER "${ARM_NONE_EABI_GPP}" CACHE FILEPATH "CXX compiler" FORCE)
endif()

SET(CMAKE_AR "${ARM_NONE_EABI_AR}" CACHE FILEPATH "Archiver")
SET(CMAKE_RANLIB "${ARM_NONE_EABI_RANLIB}" CACHE FILEPATH "rlib")
set(CMAKE_CXX_OUTPUT_EXTENSION ".o")
