// SPDX-License-Identifier: GPL-3.0-or-later
/* Production provider + commands/core; register values are simulated, not entropy evidence. */
#define main existing_command_fixture_main
#include "tests/commands/capture.c"
#undef main
#include "stm32/internal.h"
static struct rng_regs rng_regs;
static struct rcc_regs rcc_regs;
static uint32_t supplied_words[2];
static unsigned provider_reads, provider_claimed;
static uint32_t review_dr(void);
#include "provider-under-review.c"

uint32_t is_enabled_pclock(uint32_t peripheral)
{
    (void)peripheral;assert(test_shim_irq_is_disabled());return provider_claimed;
}
void enable_pclock(uint32_t peripheral)
{
    (void)peripheral;assert(!provider_claimed && test_shim_irq_is_disabled());provider_claimed=1;
    memset(&rng_regs,0,sizeof(rng_regs));
}
static uint32_t review_dr(void)
{
    assert(provider_reads<2);return supplied_words[provider_reads++];
}
static km_result_t provide(const char *mode)
{
    uint64_t value=INSTANCE;
    if (!strcmp(mode,"fresh")) value ^= UINT64_C(1)<<40;
    if (!strcmp(mode,"zero")) value=0;
    supplied_words[0]=(uint32_t)(value>>32);supplied_words[1]=(uint32_t)value;
    if (!strcmp(mode,"foreign")) provider_claimed=1;
    assert(km_boot_identity_get(&value)==KM_ERR_UNAVAILABLE);
    km_bi_init();assert(!provider_reads && !test_shim_active_timer);
    km_bi_task();
    for (unsigned step=0;test_shim_active_timer && step<25;step++) {
        assert(test_shim_active_timer==&bi_timer);
        shim_set_ticks(test_shim_active_timer->waketime);
        if (strcmp(mode,"timeout")) rcc_regs.CR|=RCC_CR_HSI48RDY;
        rng_regs.SR=RNG_SR_DRDY;
        if (!strcmp(mode,"fault") && step==2) rng_regs.SR|=RNG_SR_SEIS;
        if (!strcmp(mode,"shutdown") && step==2) {km_bi_shutdown();break;}
        assert(shim_dispatch_timer()==SF_DONE);km_bi_task();
    }
    assert(!test_shim_active_timer);
    uint64_t before=value;
    km_result_t result=km_boot_identity_get(&value);
    if(result!=KM_OK) assert(value==before);
    else {
        assert(provider_reads==2 && value==(((uint64_t)supplied_words[0]<<32)|supplied_words[1]));
        /* This is test composition, not a product automatic-startup caller. */
        shim_run_init();shim_run_task();
        assert(km_commands_attach(&core,value,changed, true)==KM_OK);
    }
    return result;
}
int main(int argc,char **argv)
{
    assert(argc==2);const char *mode=argv[1];
    bool fresh=!strcmp(mode,"fresh"), reused=!strcmp(mode,"reused");
    prepare();shim_set_board_ready(true);
    km_result_t result=provide(mode);
    if(fresh||reused) assert(result==KM_OK);
    else if(!strcmp(mode,"timeout")) assert(result==KM_ERR_TIMEOUT);
    else if(!strcmp(mode,"foreign")) assert(result==KM_ERR_CONFLICT);
    else assert(result==KM_ERR_FAULTED);
    uint32_t query[]={73};call("km_transport_query ",query);
    assert(values[0]==73 && values[4]==(uint32_t)(fresh||reused));
    uint32_t lease[]={(uint32_t)(INSTANCE>>32),(uint32_t)INSTANCE,SESSION};
    call("km_heartbeat ",lease);
    expect_result(reused?KM_COMMAND_OK:fresh?KM_COMMAND_INSTANCE_MISMATCH:KM_COMMAND_UNBOUND);
    uint32_t args[8];args_identity(args,REQUEST,KM_OP_TYPE_LOAD,2);call("km_start ",args);
    if(reused) {
        /* Negative control: a deliberately repeated simulated RNG pair recreates the old identity. */
        expect_admission(KM_REQ_ACCEPTED);
        assert(core.shared_path_owned && core.active_req_id==REQUEST && core.active_session_id==SESSION);
        advance(20,true);assert(core.event_queue.count==1);
    } else {
        expect_result(fresh?KM_COMMAND_INSTANCE_MISMATCH:KM_COMMAND_UNBOUND);
        assert(!core.shared_path_owned && changed_count==0);
        assert(core.client_session[KM_CLIENT_HOST]==0 && core.client_request[KM_CLIENT_HOST]==0);
    }
    return 0;
}
