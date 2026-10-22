def register(api):
    def wc(rt, raw):
        words = raw.split()
        return f"{len(words)} word(s)"

    api.add_command("wc", wc, "count the words in the arguments")
