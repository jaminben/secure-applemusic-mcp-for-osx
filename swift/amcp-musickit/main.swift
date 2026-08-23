// amcp-musickit — MusicKit bridge for the Apple Music MCP server.
//
// It does exactly one useful thing: add a catalog song to the user's library.
// That is the single operation the Apple Events rail cannot express (Music.app's
// `play` needs an object specifier, and an unowned catalog track has none), and
// it is the only reason the server would otherwise need an Apple Music API
// credential at all.
//
// Why this removes the credential problem entirely
// ------------------------------------------------
// `MusicDataRequest` performs an authenticated Apple Music API call with
// MusicKit supplying BOTH tokens itself, from this app's identity plus the
// user's consent. The identity is the Team ID and bundle id, validated
// server-side against an App ID that has MusicKit enabled — NOT a
// provisioning-profile entitlement. Requesting
// `com.apple.developer.musickit` in an entitlements plist actually breaks it:
// nothing grants that entitlement, so the kernel SIGKILLs the process at
// launch. It must be a proper .app with an Info.plist (bundle id +
// NSAppleMusicUsageDescription), signed with Developer ID. So:
//
//   * no developer token is embedded in the shipped app (nothing to extract),
//   * no `.p8` leaves the developer's machine,
//   * no token-broker service has to exist, be paid for, or be trusted,
//   * no 6-month expiry cliff — the entitlement does not expire,
//   * the Music User Token is minted and held by MusicKit, not by us.
//
// What MusicKit on macOS deliberately does NOT do (checked against the SDK):
// `SystemMusicPlayer` and `MusicLibrary.edit` are `@available(macOS,
// unavailable)`, so this cannot drive Music.app or mutate the library through
// the Swift API. The REST endpoint via `MusicDataRequest` is the way in, and
// playback stays where it already is — Apple Events to Music.app — so there is
// still exactly one player.
//
// Surface: three verbs, JSON on stdout, numeric ids only, no filesystem access,
// no shell, and no network of its own beyond Apple's API.
//
//   amcp-musickit status
//   amcp-musickit authorize
//   amcp-musickit add <catalogSongID>

import Foundation
import MusicKit

struct Result: Encodable {
    var ok: Bool
    var status: String?
    var id: String?
    var httpStatus: Int?
    var error: String?
}

func emit(_ result: Result) -> Never {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(result), let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print(#"{"ok":false,"error":"could not encode result"}"#)
    }
    exit(result.ok ? 0 : 1)
}

func fail(_ message: String) -> Never { emit(Result(ok: false, error: message)) }

func describe(_ status: MusicAuthorization.Status) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

/// Catalog ids are numeric. Validated rather than trusted: this value is
/// interpolated into a URL that reaches Apple's service, and a helper that
/// accepts arbitrary text is a helper that can be aimed somewhere else.
func validCatalogID(_ raw: String) -> String? {
    // ASCII digits only: Character.isNumber is true for Unicode digits such as
    // Arabic-Indic "٣٤٥", which must not reach the URL.
    guard !raw.isEmpty, raw.count <= 32,
          raw.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
    return raw
}

func cmdStatus() -> Never {
    emit(Result(ok: true, status: describe(MusicAuthorization.currentStatus)))
}

func cmdAuthorize() async -> Never {
    // Native prompt attributed to THIS app — no browser, no localhost page.
    let status = await MusicAuthorization.request()
    emit(Result(ok: status == .authorized, status: describe(status),
                error: status == .authorized ? nil : "user did not grant access"))
}

func cmdAdd(_ raw: String) async -> Never {
    let status = MusicAuthorization.currentStatus
    guard status == .authorized else {
        // Never prompt implicitly: a permission dialog must not appear in the
        // middle of an unrelated request.
        emit(Result(ok: false, status: describe(status),
                    error: "not authorized — run `authorize` first"))
    }
    guard let id = validCatalogID(raw) else { fail("catalog id must be numeric") }

    var components = URLComponents(string: "https://api.music.apple.com/v1/me/library")!
    components.queryItems = [URLQueryItem(name: "ids[songs]", value: id)]
    var request = URLRequest(url: components.url!)
    request.httpMethod = "POST"

    do {
        // MusicKit signs this with the developer token AND the user token,
        // both derived from the app identity. We never see or store either.
        let response = try await MusicDataRequest(urlRequest: request).response()
        let code = response.urlResponse.statusCode
        // 202 Accepted is the documented success for a library add.
        emit(Result(ok: (200...299).contains(code), id: id, httpStatus: code,
                    error: (200...299).contains(code) ? nil : "Apple returned \(code)"))
    } catch {
        fail(error.localizedDescription)
    }
}

let args = Array(CommandLine.arguments.dropFirst())
guard let verb = args.first else { fail("usage: amcp-musickit status|authorize|add <id>") }

switch verb {
case "status":
    cmdStatus()
case "authorize":
    await cmdAuthorize()
case "add":
    guard args.count == 2 else { fail("add needs exactly one catalog id") }
    await cmdAdd(args[1])
default:
    fail("unknown command \(verb)")
}
