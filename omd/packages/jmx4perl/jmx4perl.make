JMX4PERL := jmx4perl
JMX4PERL_VERS := 1.11
JOLOKIA_VERSION := 1.2.3
JMX4PERL_DIR := $(JMX4PERL)-$(JMX4PERL_VERS)

JMX4PERL_BUILD := $(BUILD_HELPER_DIR)/$(JMX4PERL_DIR)-build
JMX4PERL_INSTALL := $(BUILD_HELPER_DIR)/$(JMX4PERL_DIR)-install
JMX4PERL_UNPACK := $(BUILD_HELPER_DIR)/$(JMX4PERL_DIR)-unpack

.PHONY: $(JMX4PERL) $(JMX4PERL)-install $(JMX4PERL)-skel $(JMX4PERL)-clean

$(JMX4PERL): $(JMX4PERL_BUILD)

$(JMX4PERL)-install: $(JMX4PERL_INSTALL)

$(JMX4PERL_BUILD): $(JMX4PERL_UNPACK) $(PERL_MODULES_BUILD)
	export PERL5LIB=$(PACKAGE_PERL_MODULES_PERL5LIB); \
	    cd $(JMX4PERL_DIR) && $(PERL) Build.PL < /dev/null >build.log 2>&1
	cd $(JMX4PERL_DIR) && ./Build
	$(TOUCH) $@

$(JMX4PERL_INSTALL): $(JMX4PERL_BUILD)
	rm -rf $(DESTDIR)$(OMD_ROOT)/skel/etc/jmx4perl
	mkdir -p $(DESTDIR)$(OMD_ROOT)/skel/etc/jmx4perl
	rsync -a $(JMX4PERL_DIR)/config $(DESTDIR)$(OMD_ROOT)/skel/etc/jmx4perl/
	chmod 644 $(DESTDIR)$(OMD_ROOT)/skel/etc/jmx4perl/config/*.cfg
	cp -p $(JMX4PERL_DIR)/blib/script/jmx4perl $(DESTDIR)$(OMD_ROOT)/bin/
	cp -p $(JMX4PERL_DIR)/blib/script/j4psh $(DESTDIR)$(OMD_ROOT)/bin/
	cp -p $(JMX4PERL_DIR)/blib/script/jolokia $(DESTDIR)$(OMD_ROOT)/bin/
	[ -d $(DESTDIR)$(OMD_ROOT)/lib/nagios/plugins ] || mkdir -p $(DESTDIR)$(OMD_ROOT)/lib/nagios/plugins
	cp -p $(JMX4PERL_DIR)/blib/script/check_jmx4perl $(DESTDIR)$(OMD_ROOT)/lib/nagios/plugins/
	[ -d $(DESTDIR)$(OMD_ROOT)/lib/perl5/lib/perl5 ] || mkdir -p $(DESTDIR)$(OMD_ROOT)/lib/perl5/lib/perl5
	rsync -a $(JMX4PERL_DIR)/blib/lib/ $(DESTDIR)$(OMD_ROOT)/lib/perl5/lib/perl5
	mkdir -p $(DESTDIR)$(OMD_ROOT)/share/man/man1
	rsync -a $(JMX4PERL_DIR)/blib/bindoc/ $(DESTDIR)$(OMD_ROOT)/share/man/man1
	test -d $(DESTDIR)$(OMD_ROOT)/share/doc/jmx4perl || \
	        mkdir -p $(DESTDIR)$(OMD_ROOT)/share/doc/jmx4perl
	install -m 644 $(PACKAGE_DIR)/$(JMX4PERL)/README $(DESTDIR)$(OMD_ROOT)/share/doc/jmx4perl
# Jolokia Agents
	rm -rf $(DESTDIR)$(OMD_ROOT)/share/jmx4perl
	mkdir -p $(DESTDIR)$(OMD_ROOT)/share/jmx4perl/jolokia-$(JOLOKIA_VERSION)
	rsync -a $(PACKAGE_DIR)/$(JMX4PERL)/jolokia-agents/$(JOLOKIA_VERSION)/ $(DESTDIR)$(OMD_ROOT)/share/jmx4perl/jolokia-$(JOLOKIA_VERSION)/
	chmod 644 $(DESTDIR)$(OMD_ROOT)/share/jmx4perl/jolokia-$(JOLOKIA_VERSION)/*
	$(TOUCH) $@

$(JMX4PERL)-skel:

$(JMX4PERL)-clean:
	rm -rf $(JMX4PERL_DIR) $(BUILD_HELPER_DIR)/$(JMX4PERL)*
