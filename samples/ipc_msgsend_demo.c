#include <stdio.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>
#include <sys/neutrino.h>
#include "aion_markers.h"

#ifndef USE_PULSE_FIX
#define USE_PULSE_FIX 0
#endif

static int chid = -1;
static int coid = -1;

static void burn_ms(double target_ms) {
    struct timespec s, n;
    clock_gettime(CLOCK_MONOTONIC, &s);
    while (1) {
        clock_gettime(CLOCK_MONOTONIC, &n);
        double el = (n.tv_sec - s.tv_sec) * 1000.0 + (n.tv_nsec - s.tv_nsec) / 1e6;
        if (el >= target_ms) break;
    }
}

static void* server_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "diag_server");
    int req = 0, rep = 1;
    struct _pulse pulse;
    if (USE_PULSE_FIX) {
        MsgReceivePulse(chid, &pulse, sizeof(pulse), NULL);
        burn_ms(6.0);
    } else {
        rcvid_t rcvid = MsgReceive(chid, &req, sizeof(req), NULL);
        burn_ms(6.0); /* Runs at unboosted prio 10 because of _NTO_CHF_FIXED_PRIORITY! */
        MsgReply(rcvid, 0, &rep, sizeof(rep));
    }
    return NULL;
}

static void* med_worker_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "bg_worker");
    usleep(3500); /* Wakes up right after sensor_high sends IPC message to diag_server! */
    burn_ms(22.0); /* Preempts diag_server (prio 10) for 22 ms while sensor_high is THREPLY! */
    return NULL;
}

static void* client_high_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "sensor_high");
    job_begin("sensor_high");
    burn_ms(1.0);
    job_end("sensor_high");

    usleep(2000);
    job_begin("sensor_high");
    burn_ms(1.0);
    if (USE_PULSE_FIX) {
        /* Fixed: Non-blocking asynchronous pulse! Zero THREPLY wait! */
        MsgSendPulse(coid, -1, 1, 42);
    } else {
        /* Broken: Synchronous MsgSend into fixed-priority server channel! */
        int req = 42, rep = 0;
        MsgSend(coid, &req, sizeof(req), &rep, sizeof(rep));
    }
    job_end("sensor_high");
    return NULL;
}

static void spawn_rt(pthread_t* t, int prio, void* (*fn)(void*)) {
    pthread_attr_t attr;
    struct sched_param param;
    pthread_attr_init(&attr);
    pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    pthread_attr_setschedpolicy(&attr, SCHED_FIFO);
    param.sched_priority = prio;
    pthread_attr_setschedparam(&attr, &param);
    pthread_create(t, &attr, fn, NULL);
}

int main(void) {
    pin_to_cpu0();
    trace_init();
    struct sched_param sp = { .sched_priority = 55 };
    sched_setscheduler(0, SCHED_FIFO, &sp);

    /* _NTO_CHF_FIXED_PRIORITY disables QNX IPC priority inheritance on the channel! */
    chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);
    coid = ConnectAttach(0, 0, chid, _NTO_SIDE_CHANNEL, 0);

    pthread_t t_srv, t_med, t_cli;
    spawn_rt(&t_srv, 10, server_fn);
    spawn_rt(&t_med, 25, med_worker_fn);
    spawn_rt(&t_cli, 40, client_high_fn);

    pthread_join(t_cli, NULL);
    pthread_join(t_med, NULL);
    pthread_join(t_srv, NULL);
    return 0;
}