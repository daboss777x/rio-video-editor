// Rio 2.0: macOS Vision helper (no installs; ships with the OS).
//   swift vision.swift faces <list.txt>          -> one JSON line per image: {"p", "faces": [[x,y,w,h,conf,yaw]]}
//     (yaw in radians, negative = turned to the viewer's left; 0 when Vision gives none)
//   swift vision.swift text  <list.txt>          -> one JSON line per image: {"p", "text": [[x,y,w,h,"str"]]}
// list.txt holds one image path per line. Boxes are in the image's pixels, top-left origin.
import AppKit
import Foundation
import Vision

let args = CommandLine.arguments
guard args.count == 3, ["faces", "text"].contains(args[1]),
      let list = try? String(contentsOfFile: args[2], encoding: .utf8) else {
    FileHandle.standardError.write("usage: vision.swift faces|text <list.txt>\n".data(using: .utf8)!)
    exit(2)
}

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        print(s)
    }
}

for path in list.split(separator: "\n").map(String.init) where !path.isEmpty {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        emit(["p": path, "error": "unreadable"])
        continue
    }
    let W = Double(cg.width), H = Double(cg.height)
    func box(_ b: CGRect) -> [Double] {
        [(b.minX * W).rounded(), ((1 - b.maxY) * H).rounded(), (b.width * W).rounded(), (b.height * H).rounded()]
    }
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    if args[1] == "faces" {
        let req = VNDetectFaceRectanglesRequest()
        req.revision = VNDetectFaceRectanglesRequestRevision3      // yaw / pitch / roll
        try? handler.perform([req])
        let faces = (req.results ?? []).map {
            box($0.boundingBox) + [Double($0.confidence), $0.yaw?.doubleValue ?? 0.0] }
        emit(["p": path, "faces": faces])
    } else {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = false
        try? handler.perform([req])
        var out: [[Any]] = []
        for obs in req.results ?? [] {
            guard let cand = obs.topCandidates(1).first else { continue }
            // one box per word, so a name inside a longer line gets its own tight box
            let s = cand.string
            var idx = s.startIndex
            for word in s.split(separator: " ") {
                guard let r = s.range(of: word, range: idx..<s.endIndex) else { continue }
                idx = r.upperBound
                if let wb = try? cand.boundingBox(for: r) {
                    out.append(box(wb.boundingBox).map { $0 as Any } + [String(word)])
                }
            }
        }
        emit(["p": path, "text": out])
    }
}
