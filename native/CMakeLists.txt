cmake_minimum_required(VERSION 3.20)
project(aegis_native LANGUAGES C CXX)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(AQ_ENABLE_SANITIZERS "Enable ASan and UBSan" OFF)

find_package(Threads REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(OpenSSL REQUIRED)

set(AQ_COMMON_FLAGS -Wall -Wextra -Werror -fno-fast-math)
if(AQ_ENABLE_SANITIZERS)
    list(APPEND AQ_COMMON_FLAGS -fsanitize=address,undefined -fno-omit-frame-pointer -g)
    add_link_options(-fsanitize=address,undefined)
endif()

add_library(aq_core STATIC src/core.c)
target_include_directories(aq_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_options(aq_core PRIVATE ${AQ_COMMON_FLAGS} -pedantic)

add_library(aq_engine STATIC
    src/storage.cpp
    src/data.cpp
    src/model.cpp
    src/rl.cpp
    src/inference_abi.cpp
    src/oms.cpp
    src/service.cpp
)
target_include_directories(aq_engine PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(aq_engine PUBLIC aq_core SQLite::SQLite3 OpenSSL::Crypto Threads::Threads)
target_compile_options(aq_engine PRIVATE ${AQ_COMMON_FLAGS})

add_executable(aegis_native src/main.cpp)
target_link_libraries(aegis_native PRIVATE aq_engine)
target_compile_options(aegis_native PRIVATE ${AQ_COMMON_FLAGS})

enable_testing()

add_executable(aq_c_infer tests/c_abi_smoke.c)
target_link_libraries(aq_c_infer PRIVATE aq_engine m)
target_compile_options(aq_c_infer PRIVATE ${AQ_COMMON_FLAGS} -pedantic)
add_test(NAME c_abi_smoke COMMAND aq_c_infer)

add_executable(test_native tests/test_native.cpp)
target_link_libraries(test_native PRIVATE aq_engine)
target_compile_options(test_native PRIVATE ${AQ_COMMON_FLAGS})
add_test(NAME native_45_checks COMMAND test_native)

