const std = @import("std");
const linux = std.os.linux;
const c = @cImport({
    @cInclude("dirent.h");
    @cInclude("fcntl.h");
    @cInclude("stdio.h");
    @cInclude("stdlib.h");
    @cInclude("string.h");
    @cInclude("sys/prctl.h");
    @cInclude("unistd.h");
});

const PR_SET_MEMORY_MERGE: c_int = 67;

fn monoUs() u64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.MONOTONIC, &ts);
    return @as(u64, @intCast(ts.sec)) * 1_000_000 + @as(u64, @intCast(@divTrunc(ts.nsec, 1000)));
}

fn readSmallFile(path: [*:0]const u8, buf: []u8) usize {
    const fd = c.open(path, c.O_RDONLY);
    if (fd < 0) return 0;
    defer _ = c.close(fd);
    var total: usize = 0;
    while (total < buf.len) {
        const n = c.read(fd, buf[total..].ptr, buf.len - total);
        if (n <= 0) break;
        total += @as(usize, @intCast(n));
    }
    return total;
}

fn readSmallIntFile(path: [*:0]const u8) u64 {
    var buf: [64]u8 = undefined;
    const n = readSmallFile(path, &buf);
    if (n == 0) return 0;
    const trimmed = std.mem.trim(u8, buf[0..n], " \t\r\n");
    return std.fmt.parseInt(u64, trimmed, 10) catch 0;
}

fn isAllDigits(s: []const u8) bool {
    if (s.len == 0) return false;
    for (s) |ch| {
        if (ch < '0' or ch > '9') return false;
    }
    return true;
}

const ProcStat = struct {
    pid: i32,
    ppid: i32,
    comm: [64]u8,
    comm_len: usize,
};

fn parseProcStat(pid: i32, out: *ProcStat) bool {
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, "/proc/{d}/stat", .{pid}) catch return false;
    var buf: [512]u8 = undefined;
    const n = readSmallFile(path.ptr, &buf);
    if (n == 0) return false;
    const data = buf[0..n];
    const lparen = std.mem.indexOfScalar(u8, data, '(') orelse return false;
    const rparen = std.mem.lastIndexOfScalar(u8, data, ')') orelse return false;
    if (rparen <= lparen) return false;
    const comm_slice = data[lparen + 1 .. rparen];
    const copy_len = @min(comm_slice.len, out.comm.len);
    @memcpy(out.comm[0..copy_len], comm_slice[0..copy_len]);
    out.comm_len = copy_len;
    out.pid = pid;

    if (rparen + 2 >= data.len) return false;
    const rest = data[rparen + 2 ..];
    var it = std.mem.tokenizeScalar(u8, rest, ' ');
    _ = it.next(); // state
    const ppid_s = it.next() orelse return false;
    out.ppid = std.fmt.parseInt(i32, ppid_s, 10) catch return false;
    return true;
}

const SmapsInfo = struct {
    rss_kb: u64 = 0,
    pss_kb: u64 = 0,
    pss_anon_kb: u64 = 0,
    shared_clean_kb: u64 = 0,
    ksm_kb: u64 = 0,
    swap_kb: u64 = 0,
};

fn parseSmapsRollup(pid: i32) SmapsInfo {
    var info = SmapsInfo{};
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, "/proc/{d}/smaps_rollup", .{pid}) catch return info;
    var buf: [2048]u8 = undefined;
    const n = readSmallFile(path.ptr, &buf);
    if (n == 0) return info;

    var lines = std.mem.splitScalar(u8, buf[0..n], '\n');
    while (lines.next()) |line| {
        const colon = std.mem.indexOfScalar(u8, line, ':') orelse continue;
        const key = line[0..colon];
        const rest = std.mem.trim(u8, line[colon + 1 ..], " \t\r");
        var tok = std.mem.tokenizeScalar(u8, rest, ' ');
        const val_s = tok.next() orelse continue;
        const val = std.fmt.parseInt(u64, val_s, 10) catch continue;

        if (std.mem.eql(u8, key, "Rss")) {
            info.rss_kb = val;
        } else if (std.mem.eql(u8, key, "Pss")) {
            info.pss_kb = val;
        } else if (std.mem.eql(u8, key, "Pss_Anon")) {
            info.pss_anon_kb = val;
        } else if (std.mem.eql(u8, key, "Shared_Clean")) {
            info.shared_clean_kb = val;
        } else if (std.mem.eql(u8, key, "KSM")) {
            info.ksm_kb = val;
        } else if (std.mem.eql(u8, key, "SwapPss")) {
            info.swap_kb = val;
        }
    }
    return info;
}

fn findConvLockAndTty(pid: i32, conv_buf: []u8, tty_buf: []u8) struct { conv_len: usize, tty_len: usize } {
    var tty_len: usize = 0;
    var fd0_path: [64]u8 = undefined;
    if (std.fmt.bufPrintZ(&fd0_path, "/proc/{d}/fd/0", .{pid})) |p| {
        var link_buf: [256]u8 = undefined;
        const ln = c.readlink(p.ptr, &link_buf, link_buf.len);
        if (ln > 5) {
            const target = link_buf[0..@as(usize, @intCast(ln))];
            if (std.mem.startsWith(u8, target, "/dev/")) {
                const sub = target[5..];
                tty_len = @min(sub.len, tty_buf.len);
                @memcpy(tty_buf[0..tty_len], sub[0..tty_len]);
            }
        }
    } else |_| {}

    var conv_len: usize = 0;
    var fddir_path: [64]u8 = undefined;
    const fddir_z = std.fmt.bufPrintZ(&fddir_path, "/proc/{d}/fd", .{pid}) catch return .{ .conv_len = 0, .tty_len = tty_len };
    const dir = c.opendir(fddir_z.ptr);
    if (dir != null) {
        defer _ = c.closedir(dir);
        while (c.readdir(dir)) |ent| {
            const d_name = std.mem.span(@as([*:0]const u8, @ptrCast(&ent.*.d_name)));
            if (!isAllDigits(d_name)) continue;
            var fpath: [128]u8 = undefined;
            const fz = std.fmt.bufPrintZ(&fpath, "/proc/{d}/fd/{s}", .{ pid, d_name }) catch continue;
            var lbuf: [512]u8 = undefined;
            const ln = c.readlink(fz.ptr, &lbuf, lbuf.len);
            if (ln <= 0) continue;
            const target = lbuf[0..@as(usize, @intCast(ln))];
            if (std.mem.indexOf(u8, target, "/presence/") != null and std.mem.endsWith(u8, target, ".lock")) {
                const base = std.fs.path.basename(target);
                if (base.len > 5) {
                    const id_slice = base[0 .. base.len - 5];
                    conv_len = @min(id_slice.len, conv_buf.len);
                    @memcpy(conv_buf[0..conv_len], id_slice[0..conv_len]);
                    break;
                }
            }
        }
    }

    if (conv_len == 0) {
        var cmd_path: [64]u8 = undefined;
        if (std.fmt.bufPrintZ(&cmd_path, "/proc/{d}/cmdline", .{pid})) |cp| {
            var cbuf: [1024]u8 = undefined;
            const cn = readSmallFile(cp.ptr, &cbuf);
            if (cn > 0) {
                var it = std.mem.splitScalar(u8, cbuf[0..cn], 0);
                while (it.next()) |arg| {
                    if (std.mem.eql(u8, arg, "--conversation")) {
                        if (it.next()) |val| {
                            if (val.len > 0) {
                                conv_len = @min(val.len, conv_buf.len);
                                @memcpy(conv_buf[0..conv_len], val[0..conv_len]);
                                break;
                            }
                        }
                    }
                }
            }
        } else |_| {}
    }

    return .{ .conv_len = conv_len, .tty_len = tty_len };
}

fn writeJsonEscaped(out: *std.ArrayListUnmanaged(u8), gpa: std.mem.Allocator, s: []const u8) !void {
    for (s) |ch| {
        switch (ch) {
            '"' => try out.appendSlice(gpa, "\\\""),
            '\\' => try out.appendSlice(gpa, "\\\\"),
            '\n' => try out.appendSlice(gpa, "\\n"),
            '\r' => try out.appendSlice(gpa, "\\r"),
            '\t' => try out.appendSlice(gpa, "\\t"),
            else => {
                if (ch >= 0x20) try out.append(gpa, ch);
            },
        }
    }
}

fn runScan(gpa: std.mem.Allocator, statusbar_only: bool) !void {
    const t0 = monoUs();
    const proc_dir = c.opendir("/proc");
    if (proc_dir == null) return;
    defer _ = c.closedir(proc_dir);

    var stats: std.ArrayListUnmanaged(ProcStat) = .empty;
    defer stats.deinit(gpa);

    var agy_pids: std.ArrayListUnmanaged(i32) = .empty;
    defer agy_pids.deinit(gpa);

    while (c.readdir(proc_dir)) |ent| {
        const name = std.mem.span(@as([*:0]const u8, @ptrCast(&ent.*.d_name)));
        if (!isAllDigits(name)) continue;
        const pid = std.fmt.parseInt(i32, name, 10) catch continue;

        var st: ProcStat = undefined;
        if (parseProcStat(pid, &st)) {
            try stats.append(gpa, st);
        }

        var exe_path: [64]u8 = undefined;
        const ez = std.fmt.bufPrintZ(&exe_path, "/proc/{d}/exe", .{pid}) catch continue;
        var lbuf: [512]u8 = undefined;
        const ln = c.readlink(ez.ptr, &lbuf, lbuf.len);
        if (ln <= 0) continue;
        const target = lbuf[0..@as(usize, @intCast(ln))];
        const base = std.fs.path.basename(target);
        if (std.mem.eql(u8, base, "agy")) {
            try agy_pids.append(gpa, pid);
        }
    }

    const ksm_run = readSmallIntFile("/sys/kernel/mm/ksm/run");
    const ksm_shared = readSmallIntFile("/sys/kernel/mm/ksm/pages_shared");
    const ksm_sharing = readSmallIntFile("/sys/kernel/mm/ksm/pages_sharing");
    const ksm_scanned = readSmallIntFile("/sys/kernel/mm/ksm/pages_scanned");

    if (statusbar_only) {
        var total_pss_kb: u64 = 0;
        var max_shared_kb: u64 = 0;
        var total_ksm_kb: u64 = 0;
        var live_count: usize = 0;
        for (agy_pids.items) |pid| {
            const sm = parseSmapsRollup(pid);
            if (sm.rss_kb == 0) continue;
            live_count += 1;
            total_pss_kb += sm.pss_kb;
            if (sm.shared_clean_kb > max_shared_kb) max_shared_kb = sm.shared_clean_kb;
            total_ksm_kb += sm.ksm_kb;
        }
        var out_buf: [256]u8 = undefined;
        const msg = try std.fmt.bufPrint(
            &out_buf,
            "⚡Zig:{d}us · RAM:{d}M PSS · {d}M Shared+{d}M KSM · {d}🔥\n",
            .{ monoUs() - t0, total_pss_kb / 1024, max_shared_kb / 1024, total_ksm_kb / 1024, live_count },
        );
        _ = c.write(1, msg.ptr, msg.len);
        return;
    }

    var json: std.ArrayListUnmanaged(u8) = .empty;
    defer json.deinit(gpa);

    const elapsed = monoUs() - t0;
    var hdr_buf: [256]u8 = undefined;
    const hdr = try std.fmt.bufPrint(
        &hdr_buf,
        "{{\"scan_us\":{d},\"ksm\":{{\"run\":{d},\"pages_shared\":{d},\"pages_sharing\":{d},\"pages_scanned\":{d}}},\"procs\":[",
        .{ elapsed, ksm_run, ksm_shared, ksm_sharing, ksm_scanned },
    );
    try json.appendSlice(gpa, hdr);

    var first_proc = true;
    for (agy_pids.items) |pid| {
        const sm = parseSmapsRollup(pid);
        if (sm.rss_kb == 0) continue;

        var conv_buf: [128]u8 = undefined;
        var tty_buf: [64]u8 = undefined;
        const found = findConvLockAndTty(pid, &conv_buf, &tty_buf);
        const conv_slice = conv_buf[0..found.conv_len];
        const tty_slice = if (found.tty_len > 0) tty_buf[0..found.tty_len] else "?";

        var ppid: i32 = 0;
        for (stats.items) |st| {
            if (st.pid == pid) {
                ppid = st.ppid;
                break;
            }
        }

        if (!first_proc) try json.append(gpa, ',');
        first_proc = false;

        var pbuf: [512]u8 = undefined;
        const pstr = try std.fmt.bufPrint(
            &pbuf,
            "{{\"pid\":{d},\"ppid\":{d},\"tty\":\"",
            .{ pid, ppid },
        );
        try json.appendSlice(gpa, pstr);
        try writeJsonEscaped(&json, gpa, tty_slice);
        try json.appendSlice(gpa, "\",\"conv_id\":\"");
        try writeJsonEscaped(&json, gpa, conv_slice);

        const mstr = try std.fmt.bufPrint(
            &pbuf,
            "\",\"rss_kb\":{d},\"pss_kb\":{d},\"pss_anon_kb\":{d},\"shared_clean_kb\":{d},\"ksm_kb\":{d},\"swap_kb\":{d},\"children\":[",
            .{ sm.rss_kb, sm.pss_kb, sm.pss_anon_kb, sm.shared_clean_kb, sm.ksm_kb, sm.swap_kb },
        );
        try json.appendSlice(gpa, mstr);

        var first_child = true;
        for (stats.items) |st| {
            if (st.ppid == pid) {
                const cname = st.comm[0..st.comm_len];
                if (std.mem.eql(u8, cname, "isymcp-hub")) continue;
                if (!first_child) try json.append(gpa, ',');
                first_child = false;
                try json.append(gpa, '"');
                try writeJsonEscaped(&json, gpa, cname);
                var cbuf: [32]u8 = undefined;
                const cstr = try std.fmt.bufPrint(&cbuf, "({d})\"", .{st.pid});
                try json.appendSlice(gpa, cstr);
            }
        }
        try json.appendSlice(gpa, "]}");
    }

    try json.appendSlice(gpa, "]}\n");
    _ = c.write(1, json.items.ptr, json.items.len);
}

fn collectTailLines(gpa: std.mem.Allocator, path_slice: []const u8, max_lines: usize, out: *std.ArrayListUnmanaged(u8)) !void {
    const path_z = try gpa.dupeZ(u8, path_slice);
    defer gpa.free(path_z);

    const fd = c.open(path_z.ptr, c.O_RDONLY);
    if (fd < 0) return;
    defer _ = c.close(fd);

    const end_pos = c.lseek(fd, 0, c.SEEK_END);
    if (end_pos <= 0) return;

    const window: c.off_t = 262_144; // 256 KB tail window
    const start_pos: c.off_t = if (end_pos > window) end_pos - window else 0;
    _ = c.lseek(fd, start_pos, c.SEEK_SET);

    const read_len: usize = @intCast(end_pos - start_pos);
    const buf = try gpa.alloc(u8, read_len);
    defer gpa.free(buf);

    var total: usize = 0;
    while (total < read_len) {
        const n = c.read(fd, buf[total..].ptr, read_len - total);
        if (n <= 0) break;
        total += @as(usize, @intCast(n));
    }
    const slice = buf[0..total];

    var lines: std.ArrayListUnmanaged([]const u8) = .empty;
    defer lines.deinit(gpa);

    var it = std.mem.splitScalar(u8, slice, '\n');
    if (start_pos > 0) {
        // Discard partial first line when seeking into middle of file
        _ = it.next();
    }
    while (it.next()) |line| {
        const trimmed = std.mem.trim(u8, line, "\r ");
        if (trimmed.len > 0 and trimmed[0] == '{') {
            try lines.append(gpa, trimmed);
        }
    }

    const count = lines.items.len;
    const from = if (count > max_lines) count - max_lines else 0;
    for (lines.items[from..]) |l| {
        try out.appendSlice(gpa, l);
        try out.append(gpa, '\n');
    }
}

fn runTailLines(gpa: std.mem.Allocator, path_slice: []const u8, max_lines: usize) !void {
    var out: std.ArrayListUnmanaged(u8) = .empty;
    defer out.deinit(gpa);
    try collectTailLines(gpa, path_slice, max_lines, &out);
    if (out.items.len > 0) {
        _ = c.write(1, out.items.ptr, out.items.len);
    }
}

pub fn main(init: std.process.Init) !void {
    const gpa = std.heap.c_allocator;
    var args_it = try std.process.Args.iterateAllocator(init.minimal.args, gpa);
    defer args_it.deinit();

    _ = args_it.next(); // argv[0]
    const cmd = args_it.next() orelse "scan";

    if (std.mem.eql(u8, cmd, "scan")) {
        try runScan(gpa, false);
    } else if (std.mem.eql(u8, cmd, "statusbar")) {
        try runScan(gpa, true);
    } else if (std.mem.eql(u8, cmd, "tail-lines")) {
        const p = args_it.next() orelse return;
        const max_s = args_it.next() orelse "40";
        const max_n = std.fmt.parseInt(usize, max_s, 10) catch 40;
        try runTailLines(gpa, p, max_n);
    } else if (std.mem.eql(u8, cmd, "ksm-exec")) {
        _ = c.prctl(PR_SET_MEMORY_MERGE, @as(c_ulong, 1), @as(c_ulong, 0), @as(c_ulong, 0), @as(c_ulong, 0));
        var argv_list: std.ArrayListUnmanaged(?[*:0]const u8) = .empty;
        defer argv_list.deinit(gpa);
        while (args_it.next()) |a| {
            const dup = try gpa.dupeZ(u8, a);
            try argv_list.append(gpa, dup.ptr);
        }
        if (argv_list.items.len == 0) return;
        try argv_list.append(gpa, null);
        _ = c.execvp(argv_list.items[0].?, @ptrCast(argv_list.items.ptr));
        std.process.exit(127);
    } else {
        try runScan(gpa, false);
    }
}

test "isAllDigits validates pid strings" {
    try std.testing.expect(isAllDigits("12345"));
    try std.testing.expect(!isAllDigits(""));
    try std.testing.expect(!isAllDigits("12a45"));
    try std.testing.expect(!isAllDigits("self"));
}

test "writeJsonEscaped escapes quotes and control chars" {
    const gpa = std.testing.allocator;
    var out: std.ArrayListUnmanaged(u8) = .empty;
    defer out.deinit(gpa);
    try writeJsonEscaped(&out, gpa, "a\"b\\c\nd");
    try std.testing.expectEqualStrings("a\\\"b\\\\c\\nd", out.items);
}

test "collectTailLines extracts valid JSONL tail lines" {
    const gpa = std.testing.allocator;
    const tmp_path = "/tmp/agy_fast_core_test_tail.jsonl";
    {
        const wfd = c.open(tmp_path, c.O_WRONLY | c.O_CREAT | c.O_TRUNC, @as(c_uint, 0o644));
        try std.testing.expect(wfd >= 0);
        defer _ = c.close(wfd);
        const payload = "{\"a\":1}\nnot-json\n{\"a\":2}\n{\"a\":3}\n";
        _ = c.write(wfd, payload.ptr, payload.len);
    }
    defer _ = c.unlink(tmp_path);

    var out: std.ArrayListUnmanaged(u8) = .empty;
    defer out.deinit(gpa);
    try collectTailLines(gpa, tmp_path, 2, &out);
    try std.testing.expectEqualStrings("{\"a\":2}\n{\"a\":3}\n", out.items);
}
