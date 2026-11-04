// Spike 4's second player: list an MP3's chapters the way AVFoundation (Apple's media stack)
// reads them, after the duration and top-level tags. `swift avchapters.swift <file.mp3>` prints one line per chapter: start, title,
// artwork bytes.
import AVFoundation

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let asset = AVURLAsset(url: url)
let sem = DispatchSemaphore(value: 0)
Task {
    defer { sem.signal() }
    let groups: [AVTimedMetadataGroup]
    do {
        groups = try await asset.loadChapterMetadataGroups(bestMatchingPreferredLanguages: ["en"])
    } catch {
        print("error: \(error)")
        return
    }
    // AVFoundation lists no ID3 chapters for an MP3 (spike 4: 0 for Python's file too), so the
    // top-level tags and the duration are what it can compare.
    if let (duration, common) = try? await asset.load(.duration, .commonMetadata) {
        print(String(format: "duration: %.3f", duration.seconds))
        for item in common {
            let key = item.commonKey?.rawValue ?? "?"
            if let s = try? await item.load(.stringValue) { print(key, s) }
            else if let d = try? await item.load(.dataValue) { print(key, d.count, "bytes") }
        }
    }
    print("chapters: \(groups.count)")
    for g in groups {
        var title = "", art = 0
        for item in g.items {
            if item.commonKey == .commonKeyTitle { title = (try? await item.load(.stringValue)) ?? "" }
            if item.commonKey == .commonKeyArtwork { art = (try? await item.load(.dataValue))?.count ?? 0 }
        }
        print(String(format: "%.3f", g.timeRange.start.seconds), title, art)
    }
}
sem.wait()
