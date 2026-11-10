/*
 *  Aion user trace marker header
 *  Zero-dependency helper macros for marking real-time job releases and completions inside QNX 8.0 kernel trace stream.
*/

#ifndef MARKERS_H
#define MARKERS_H

#include <stdint.h>
#include <sys/neutrino.h>
#include <sys/trace.h>

#define EVENT_JOB_BEGIN 100
#define EVENT_JOB_END 101

static inline void trace_init(void) {
    /* Ensure QNX kernel trace filter includes user events */
    TraceEvent(_NTO_TRACE_ADDCLASS, _NTO_TRACE_USER);
}

static inline void pin_to_cpu0(void) {
    /* Pin calling thread strictly to CPU 0 so threads compete on a single core */
    unsigned int runmask = 0x1;
    ThreadCtl(_NTO_TCTL_RUNMASK, &runmask);
}

static inline void job_begin(const char* thread_name) {
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, EVENT_JOB_BEGIN, thread_name);
}

static inline void job_end(const char* thread_name) {
    TraceEvent(_NTO_TRACE_INSERTUSRSTREVENT, EVENT_JOB_END, thread_name);
}

#endif /* MARKERS_H*/