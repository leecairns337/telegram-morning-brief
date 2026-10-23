// One request to Apple's on-device model (Apple Intelligence), for
// telegram_brief.py. The Foundation Models framework is Swift-only, so the
// Python side talks to this tiny program over stdin/stdout.
//
//   in  (stdin, JSON):  {"system": "...", "prompt": "...",
//                        "temperature": 0.0, "max_tokens": 500}
//   out (stdout):       the model's reply as plain text
//   exit codes:         0 ok
//                       2 model unavailable (Apple Intelligence off / not ready)
//                       3 blocked by Apple's safety guardrails
//                       4 prompt too long for the context window
//                       1 anything else (message on stderr)
//
// Plain text only, on purpose: guided generation (@Generable) always runs under
// the strict default guardrails, which block most overnight war reporting.
//
// Built by setup.sh:  swiftc -O apple_llm.swift -o apple_llm
// Check-only mode:    ./apple_llm --check   (prints availability, exit 0/2)

import Foundation
import FoundationModels

struct Request: Decodable {
    let system: String
    let prompt: String
    let temperature: Double?
    let max_tokens: Int?
}

func fail(_ code: Int32, _ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

// Overnight war reporting trips the default guardrails constantly. The brief
// only selects or restates the messages it is given, which is exactly what the
// permissive "content transformations" setting is for.
let model = SystemLanguageModel(guardrails: .permissiveContentTransformations)

guard case .available = model.availability else {
    fail(2, "Apple Intelligence model unavailable: \(model.availability)")
}
if CommandLine.arguments.contains("--check") {
    print("available")
    exit(0)
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let req = try? JSONDecoder().decode(Request.self, from: input) else {
    fail(1, "bad request JSON on stdin")
}

let session = LanguageModelSession(model: model, instructions: req.system)
let temperature = req.temperature ?? 0.0
let options = GenerationOptions(
    samplingMode: temperature == 0 ? .greedy : nil,
    temperature: temperature,
    maximumResponseTokens: req.max_tokens ?? 500
)

// macOS 27 throws LanguageModelError; macOS 26 throws the older GenerationError.
// Map both to the same exit codes so the Python side needn't care which.
func exitCode(for error: Error) -> Int32 {
    if #available(macOS 27.0, *), let err = error as? LanguageModelError {
        switch err {
        case .contextSizeExceeded: return 4
        case .guardrailViolation, .refusal: return 3
        default: return 1
        }
    }
    if let err = error as? LanguageModelSession.GenerationError {
        switch err {
        case .exceededContextWindowSize: return 4
        case .guardrailViolation, .refusal: return 3
        case .assetsUnavailable: return 2
        default: return 1
        }
    }
    return 1
}

do {
    let response = try await session.respond(to: req.prompt, options: options)
    print(response.content)
} catch {
    fail(exitCode(for: error), "generation failed: \(error)")
}
