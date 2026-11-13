//! Signals. Two kinds:
//!
//! - SIGTERM, SIGINT, SIGHUP: something outside asked the app to stop. The app logs which one, then ends by that
//!   signal exactly as it would have without this code.
//! - SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT: a crash below Rust (a native library, a stack that ran out, an
//!   abort). No panic hook sees these. The handler writes a small raw note with the backtrace, using only calls
//!   that are safe inside a signal handler, then lets the crash go on (to whatever handler was there before, so
//!   the system's crash report is still made). The next start turns the note into a crash record.

use std::ffi::CString;
use std::os::raw::{c_int, c_void};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicPtr, AtomicUsize, Ordering};

pub fn name(signal: c_int) -> String {
    match signal {
        libc::SIGTERM => "SIGTERM".into(),
        libc::SIGINT => "SIGINT".into(),
        libc::SIGHUP => "SIGHUP".into(),
        libc::SIGSEGV => "SIGSEGV".into(),
        libc::SIGBUS => "SIGBUS".into(),
        libc::SIGILL => "SIGILL".into(),
        libc::SIGFPE => "SIGFPE".into(),
        libc::SIGABRT => "SIGABRT".into(),
        other => format!("signal {other}"),
    }
}

static QUIT_PIPE: AtomicI32 = AtomicI32::new(-1);

extern "C" fn on_quit_signal(signal: c_int) {
    let fd = QUIT_PIPE.load(Ordering::Relaxed);
    if fd >= 0 {
        let byte = signal as u8;
        // SAFETY: write(2) is async-signal-safe; the pipe stays open for the life of the process.
        unsafe { libc::write(fd, (&raw const byte).cast::<c_void>(), 1) };
    }
}

/// From now on SIGTERM, SIGINT and SIGHUP call `before_exit` with the signal's name (on a thread of their own, so
/// it may log and write files), and then end the process by that same signal. A signal that was being ignored
/// when the app started stays ignored.
pub fn watch_quit_signals(before_exit: impl Fn(&str) + Send + 'static) {
    let mut fds = [0 as c_int; 2];
    // SAFETY: plain libc calls with valid arguments.
    unsafe {
        if libc::pipe(fds.as_mut_ptr()) != 0 {
            return;
        }
        libc::fcntl(fds[0], libc::F_SETFD, libc::FD_CLOEXEC);
        libc::fcntl(fds[1], libc::F_SETFD, libc::FD_CLOEXEC);
    }
    QUIT_PIPE.store(fds[1], Ordering::Relaxed);
    for signal in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        // SAFETY: sigaction with zeroed, then filled, structs; the handler only calls write(2).
        unsafe {
            let mut now: libc::sigaction = std::mem::zeroed();
            if libc::sigaction(signal, std::ptr::null(), &mut now) != 0 || now.sa_sigaction == libc::SIG_IGN {
                continue;
            }
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = on_quit_signal as extern "C" fn(c_int) as usize;
            action.sa_flags = libc::SA_RESTART;
            libc::sigemptyset(&mut action.sa_mask);
            libc::sigaction(signal, &action, std::ptr::null_mut());
        }
    }
    let read = fds[0];
    let _ = std::thread::Builder::new().name("captain-signals".into()).spawn(move || {
        let mut byte = 0u8;
        loop {
            // SAFETY: reading one byte into a one-byte buffer.
            let n = unsafe { libc::read(read, (&raw mut byte).cast::<c_void>(), 1) };
            if n == 1 {
                break;
            }
            if n == 0 || std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
                return;
            }
        }
        let signal = c_int::from(byte);
        before_exit(&name(signal));
        // SAFETY: back to the default action, then the same signal again: the process ends as it would have.
        unsafe {
            libc::signal(signal, libc::SIG_DFL);
            libc::raise(signal);
        }
    });
}

const FATAL: [c_int; 5] = [libc::SIGSEGV, libc::SIGBUS, libc::SIGILL, libc::SIGFPE, libc::SIGABRT];

static RAW_PATH: AtomicPtr<libc::c_char> = AtomicPtr::new(std::ptr::null_mut());
static HEADER: AtomicPtr<u8> = AtomicPtr::new(std::ptr::null_mut());
static HEADER_LEN: AtomicUsize = AtomicUsize::new(0);
static IN_FATAL: AtomicBool = AtomicBool::new(false);
/// What handled each fatal signal before us, to hand the crash on to.
static mut BEFORE: [Option<libc::sigaction>; 5] = [None; 5];

/// Decimal digits of `n` into `buf`; the slice that holds them. No allocation: safe in a signal handler.
fn digits(mut n: u64, buf: &mut [u8; 20]) -> &[u8] {
    let mut at = buf.len();
    loop {
        at -= 1;
        buf[at] = b'0' + (n % 10) as u8;
        n /= 10;
        if n == 0 {
            break;
        }
    }
    &buf[at..]
}

unsafe fn put(fd: c_int, bytes: &[u8]) {
    // SAFETY: write(2) is async-signal-safe.
    unsafe { libc::write(fd, bytes.as_ptr().cast::<c_void>(), bytes.len()) };
}

extern "C" fn on_fatal_signal(signal: c_int, info: *mut libc::siginfo_t, context: *mut c_void) {
    // SAFETY: only async-signal-safe calls (open, write, close, time, backtrace into a fixed buffer, sigaction,
    // raise), on memory prepared before the handler was installed.
    unsafe {
        let path = RAW_PATH.load(Ordering::Relaxed);
        if !path.is_null() && !IN_FATAL.swap(true, Ordering::Relaxed) {
            let fd = libc::open(path, libc::O_WRONLY | libc::O_CREAT | libc::O_TRUNC, 0o600 as c_int);
            if fd >= 0 {
                put(fd, std::slice::from_raw_parts(HEADER.load(Ordering::Relaxed), HEADER_LEN.load(Ordering::Relaxed)));
                let mut buf = [0u8; 20];
                put(fd, b"signal: ");
                put(fd, digits(signal as u64, &mut buf));
                put(fd, b"\nepoch: ");
                put(fd, digits(libc::time(std::ptr::null_mut()) as u64, &mut buf));
                put(fd, b"\n---- backtrace ----\n");
                #[cfg(any(target_vendor = "apple", all(target_os = "linux", target_env = "gnu")))]
                {
                    let mut frames = [std::ptr::null_mut::<c_void>(); 96];
                    let n = libc::backtrace(frames.as_mut_ptr(), frames.len() as c_int);
                    libc::backtrace_symbols_fd(frames.as_ptr(), n, fd);
                }
                libc::close(fd);
            }
        }
        // Hand the crash on. Rust's own handler (stack overflow) was there before for SIGSEGV and SIGBUS.
        let index = FATAL.iter().position(|s| *s == signal);
        let before = index.and_then(|i| (&raw const BEFORE).cast::<Option<libc::sigaction>>().add(i).read());
        match before {
            Some(old) if old.sa_sigaction != libc::SIG_DFL && old.sa_sigaction != libc::SIG_IGN => {
                libc::sigaction(signal, &old, std::ptr::null_mut());
                if old.sa_flags & libc::SA_SIGINFO != 0 {
                    let handler: extern "C" fn(c_int, *mut libc::siginfo_t, *mut c_void) = std::mem::transmute(old.sa_sigaction);
                    handler(signal, info, context);
                } else {
                    let handler: extern "C" fn(c_int) = std::mem::transmute(old.sa_sigaction);
                    handler(signal);
                }
            }
            _ => {
                libc::signal(signal, libc::SIG_DFL);
                libc::raise(signal);
            }
        }
    }
}

/// From now on a fatal signal leaves `fatal-<run>.raw` in `dir` before the process dies. `header` is put first
/// (`version: ...` lines), since nothing can be formatted inside the handler.
pub fn watch_fatal_signals(dir: &Path, run: &str, header: &str) {
    let Ok(path) = CString::new(dir.join(format!("fatal-{run}.raw")).to_string_lossy().into_owned()) else { return };
    let header = header.as_bytes().to_vec().leak();
    HEADER_LEN.store(header.len(), Ordering::Relaxed);
    HEADER.store(header.as_mut_ptr(), Ordering::Relaxed);
    RAW_PATH.store(path.into_raw(), Ordering::Relaxed);
    // The first backtrace() call may load the unwinder, which is not safe inside a handler: do it now.
    #[cfg(any(target_vendor = "apple", all(target_os = "linux", target_env = "gnu")))]
    // SAFETY: a valid buffer of one frame.
    unsafe {
        let mut frame = [std::ptr::null_mut::<c_void>(); 1];
        libc::backtrace(frame.as_mut_ptr(), 1);
    }
    for (index, signal) in FATAL.iter().enumerate() {
        // SAFETY: called once at start, before other threads install handlers; structs are zeroed then filled.
        unsafe {
            let mut old: libc::sigaction = std::mem::zeroed();
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = on_fatal_signal as extern "C" fn(c_int, *mut libc::siginfo_t, *mut c_void) as usize;
            action.sa_flags = libc::SA_SIGINFO | libc::SA_ONSTACK;
            libc::sigemptyset(&mut action.sa_mask);
            if libc::sigaction(*signal, &action, &mut old) == 0 {
                (&raw mut BEFORE).cast::<Option<libc::sigaction>>().add(index).write(Some(old));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_are_written_without_allocating() {
        let mut buf = [0u8; 20];
        assert_eq!(digits(0, &mut buf), b"0");
        assert_eq!(digits(11, &mut buf), b"11");
        assert_eq!(digits(1_794_565_353, &mut buf), b"1794565353");
        assert_eq!(digits(u64::MAX, &mut buf), b"18446744073709551615");
    }
}
