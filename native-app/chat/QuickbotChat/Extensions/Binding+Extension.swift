//
//  Binding+Extension.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 22/12/2023.
//

import SwiftUI

extension Binding {
    func onChange(_ handler: @escaping (Value) -> Void) -> Binding<Value> {
        Binding(
            get: { self.wrappedValue },
            set: { newValue in
                self.wrappedValue = newValue
                handler(newValue)
            }
        )
    }
}
