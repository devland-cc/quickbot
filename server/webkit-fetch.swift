// webkit-fetch: loads a URL in an offscreen WKWebView (real WebKit, real JS)
// and prints the result of a JavaScript expression evaluated on the page.
//
// Usage: webkit-fetch <url> [--js <expression>] [--timeout <seconds>]
//
// Default expression: document.body.innerText. If the expression returns an
// object it is serialized as JSON. Exit codes: 0 ok, 1 load/eval failure,
// 2 usage error.

import Foundation
import WebKit

var url: URL?
var js = "document.body.innerText"
var timeout: TimeInterval = 20

var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let arg = args.removeFirst()
    switch arg {
    case "--js":
        guard !args.isEmpty else { exit(2) }
        js = args.removeFirst()
    case "--timeout":
        guard !args.isEmpty, let t = TimeInterval(args.first!) else { exit(2) }
        timeout = t
        args.removeFirst()
    default:
        url = URL(string: arg)
    }
}
guard let url else {
    FileHandle.standardError.write(Data("usage: webkit-fetch <url> [--js <expression>] [--timeout <seconds>]\n".utf8))
    exit(2)
}

final class Fetcher: NSObject, WKNavigationDelegate {
    let webView: WKWebView
    let js: String

    init(js: String) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        self.webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 900),
                                 configuration: config)
        self.js = js
        super.init()
        webView.navigationDelegate = self
        webView.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
    }

    func load(_ url: URL) {
        webView.load(URLRequest(url: url))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Give client-side rendering a moment to settle before extracting.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { self.extract(attempt: 0) }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        fail(error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        fail(error.localizedDescription)
    }

    private func extract(attempt: Int) {
        webView.evaluateJavaScript(js) { result, error in
            if let error {
                // The page may still be mutating; retry briefly before giving up.
                if attempt < 3 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                        self.extract(attempt: attempt + 1)
                    }
                    return
                }
                self.fail(error.localizedDescription)
                return
            }
            let output: String
            if let s = result as? String {
                output = s
            } else if let result,
                      let data = try? JSONSerialization.data(withJSONObject: result, options: []),
                      let s = String(data: data, encoding: .utf8) {
                output = s
            } else {
                output = ""
            }
            print(output)
            exit(0)
        }
    }

    private func fail(_ message: String) {
        FileHandle.standardError.write(Data("webkit-fetch: \(message)\n".utf8))
        exit(1)
    }
}

let fetcher = Fetcher(js: js)
fetcher.load(url)
DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
    FileHandle.standardError.write(Data("webkit-fetch: timed out\n".utf8))
    exit(1)
}
RunLoop.main.run()
