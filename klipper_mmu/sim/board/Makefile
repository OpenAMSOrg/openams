dirs-y += src/linux src/generic src/simboard

src-y += linux/console.c
src-y += generic/crc16_ccitt.c generic/alloc.c
src-y += simboard/timer.c simboard/gpio.c simboard/diff_model.c simboard/plant.c simboard/tmc2209.c simboard/h723_resources_sim.c simboard/boot_identity_sim.c simboard/dict_stubs.c simboard/control.c

src-y := $(filter-out stm32/%.c generic/armcm_%.c lib/stm32h7/%.c klipper_mmu/mcu/h723_resources.c klipper_mmu/mcu/boot_identity_h723.c, $(src-y))
src-y := $(filter-out linux/main.c linux/watchdog.c, $(src-y))
OBJS_klipper.elf += $(OUT)src/linux/main.o $(OUT)src/linux/watchdog.o $(OUT)src/simboard/stubs.o





CFLAGS += -m32 -funsigned-char -Isrc/simboard
CFLAGS_klipper.elf += -lutil -lrt -lpthread

