#include <stdio.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>
#include <sys/neutrino.h>
#include <sys/trace.h>

pthread_mutex_t lock;

void pin_to_cpu0(void) {
    unsigned runmask = 0x1; //pin calling thread to cpu 0
    ThreadCtl(_NTO_TCTL_RUNMASK, &runmask);
}

void burn_ms(int ms) {
    struct timespec s, n;
    clock_gettime(CLOCK_MONOTONIC, &s);
    while (1) {
        clock_gettime(CLOCK_MONOTONIC, &n);
        double el = (n.tv_sec - s.tv_sec) * 1000.0 + (n.tv_nsec - s.tv_nsec) / 1e6;
        if (el >= ms) break;
    }
}

void* low_logger(void* arg) {
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "logger_low");
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, 10, "prio:10:logger_low_lock");
    pthread_mutex_lock(&lock);
    burn_ms(50); // hold lock for 50ms cpu time
    pthread_mutex_unlock(&lock);
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, 11, "prio:10:logger_low_unlock");
    return NULL;
}

void* med_worker(void* arg) {
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "worker_med");
    usleep(10000); // wait 10ms so logger_low grabs the lock first
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, 25, "prio:25:worker_med_start");
    burn_ms(100);  // preempt logger_low on cpu 0 for 100ms
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, 26, "prio:25:worker_med_end");
    return NULL;
}

void* high_sensor(void* arg) {
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "sensor_high");
    usleep(20000); // wake up at 20ms while worker_med is running and logger_low holds lock
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, 100, "tg_job_begin:sensor_high:prio=40");
    pthread_mutex_lock(&lock); // MUST BLOCK IN KERNEL (THMUTEX)
    pthread_mutex_unlock(&lock);
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, 101, "tg_job_end:sensor_high:prio=40");
    return NULL;
}

void create_rt_thread(pthread_t* t, int prio, void* (*fn)(void*)) {
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

    // boost main() to priority 50 so it spawns all 3 threads atomically before they run
    struct sched_param sp = { .sched_priority = 50 };
    sched_setscheduler(0, SCHED_FIFO, &sp);

    pthread_mutexattr_t mattr;
    pthread_mutexattr_init(&mattr);
    pthread_mutexattr_setprotocol(&mattr, PTHREAD_PRIO_NONE);
    pthread_mutex_init(&lock, &mattr);

    // Enable _NTO_TRACE_USER so TraceEvent strings are recorded
    TraceEvent(_NTO_TRACE_ADDCLASS, _NTO_TRACE_USER);

    pthread_t t_low, t_med, t_high;
    create_rt_thread(&t_low, 10, low_logger);
    create_rt_thread(&t_med, 25, med_worker);
    create_rt_thread(&t_high, 40, high_sensor);

    // Now main() blocks in pthread_join, letting high/med/low run on CPU 0
    pthread_join(t_low, NULL);
    pthread_join(t_med, NULL);
    pthread_join(t_high, NULL);
    printf("Priority inversion spike complete!\n");
    return 0;
}